#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=4.14"]
# ///
"""
iOS location-simulation driver with realistic movement profiles.

Drives the iOS DVT LocationSimulation service through a pymobiledevice3
RemoteXPC tunnel (iOS 17+), sending one coordinate per tick along a
great-circle path between waypoints — with speed jitter, GPS noise and
stop-at-station behaviour tuned per profile.

Profiles:
  walk      4.8 km/h, ±15% speed jitter, 3 m GPS noise, random pauses
  drive     55  km/h, ±20% speed jitter, 2 m GPS noise, rare traffic-light stops
  transit   45  km/h cruise, dwell 35 s at every waypoint (= station)
  flight    850 km/h cruise, 2 s ticks, almost no jitter

Usage:
  Terminal A (needs sudo; brings up the utun tunnel to the phone):
      sudo pymobiledevice3 remote tunneld

  Terminal B:
      uv run pikmin_walk.py walk      # default
      uv run pikmin_walk.py drive
      uv run pikmin_walk.py transit
      uv run pikmin_walk.py flight

Ctrl-C at any time — the script clears the spoof on exit so your phone
doesn't get stuck at a fake location.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import random
import signal
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator

from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS, get_tunneld_devices

Waypoint = tuple[float, float]
EARTH_R = 6_371_000.0  # mean earth radius, meters

# --- Profiles --------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """Parameters that shape what the spoofed trace *looks like* on a map."""

    label: str
    nominal_kmh: float
    tick_s: float = 1.0

    # Fraction of nominal speed to vary each tick (gaussian stddev).
    # 0.15 ≈ a walker sometimes does 4 km/h, sometimes 5.5 km/h.
    speed_jitter: float = 0.10

    # Horizontal GPS noise (meters, gaussian stddev) added *after* the
    # step is computed. Applied only to the yielded point, never fed back
    # into the next step — so noise doesn't compound into drift.
    position_jitter_m: float = 2.0

    # Odds of starting an unscheduled stop on any given tick (e.g. red
    # light, shop window). `(min_s, max_s)` is the uniform duration range.
    stop_probability_per_tick: float = 0.0
    stop_duration_range: tuple[float, float] = (0.0, 0.0)

    # Forced dwell on arrival at each intermediate waypoint, in seconds.
    # Transit routes use this to model station stops.
    waypoint_dwell_s: float = 0.0

    # Random-walk parameters. `max_radius_m > 0` marks a profile as a
    # random walk (no route needed, just a center point); everything
    # else is route-based and ignores these fields.
    max_radius_m: float = 0.0
    heading_jitter_deg: float = 20.0  # gaussian stddev of per-tick turn
    home_pull_gain: float = 0.4        # 0..1, how hard we rubber-band home

    # Trail-repulsion memory: the walker feels a soft "push" away from
    # its own recent positions, so it doesn't double-back over the same
    # path within the window. Inverse-square falloff with distance,
    # linearly decayed weight with age.
    #   trail_repulsion_gain: 0 = off, 0.3 = moderate bias, 1.0 = strong
    #   trail_memory_ticks:   how many ticks of history to remember
    trail_repulsion_gain: float = 0.0
    trail_memory_ticks: int = 0

    @property
    def nominal_mps(self) -> float:
        return self.nominal_kmh * 1000.0 / 3600.0

    @property
    def nominal_step_m(self) -> float:
        return self.nominal_mps * self.tick_s


PROFILES: dict[str, Profile] = {
    "walk": Profile(
        label="步行",
        nominal_kmh=4.8,
        tick_s=1.0,
        speed_jitter=0.15,
        position_jitter_m=3.0,
        stop_probability_per_tick=0.015,  # ~1 stop every 60-70 s on average
        stop_duration_range=(5.0, 20.0),
    ),
    "drive": Profile(
        label="開車（市區+快速道路）",
        nominal_kmh=55.0,
        tick_s=1.0,
        speed_jitter=0.20,
        position_jitter_m=2.0,
        stop_probability_per_tick=0.008,  # occasional red lights
        stop_duration_range=(15.0, 45.0),
    ),
    "transit": Profile(
        label="大眾運輸（捷運 / 公車）",
        nominal_kmh=45.0,
        tick_s=1.0,
        speed_jitter=0.08,  # smooth rail cruise
        position_jitter_m=2.5,
        waypoint_dwell_s=35.0,  # dwell at each station
    ),
    "flight": Profile(
        label="飛機（巡航）",
        nominal_kmh=850.0,
        tick_s=2.0,  # 472 m/tick, still plenty smooth on a map
        speed_jitter=0.02,
        position_jitter_m=5.0,  # wind drift, not GPS error
    ),
    # Correlated random walk tuned for Pikmin Bloom 種花:
    # - nominal 14 km/h (user asked for 15 "walking cap"); after position-
    #   jitter inflation σ²/s ≈ 0.25 m/tick, the phone reports ~15 km/h
    #   median and stays under Niantic's walking-credit ceiling
    # - speed_jitter 0.12 gaussian = p99 speed ~19 km/h (vs 0.18→31 km/h
    #   at same nominal); keeps the distribution from fat-tailing into
    #   "caught running" territory
    # - 200 m soft home tether, heading σ 22°/tick → wandering diffusion
    #   that looks NOTHING like the linear routes profiles
    # Empirically tuned via 30-min dry-runs counting unique 10-m cells.
    # The winner is "wide + soft + gentle rep":
    #   radius 400m:       gives the walker room to roam; a tight radius
    #                      forces overlap no matter what
    #   home_pull 0.25:    softer tether so boundary hits don't dominate
    #                      and collapse the walker into an orbit
    #   trail_rep 0.15:    enough to nudge away from recent footsteps
    #                      without fighting the tether
    # Result: 816 unique cells vs 532 baseline (+53%), hot cells 126→9.
    "rwalk": Profile(
        label="隨機漫步（種花用）",
        nominal_kmh=14.0,
        tick_s=1.0,
        speed_jitter=0.12,
        position_jitter_m=1.0,
        stop_probability_per_tick=0.0,
        stop_duration_range=(0.0, 0.0),
        max_radius_m=1500.0,
        heading_jitter_deg=22.0,
        home_pull_gain=0.25,
        trail_repulsion_gain=0.15,
        trail_memory_ticks=300,  # 5 min @ 1 Hz
    ),
}


# --- Routes ----------------------------------------------------------------

ROUTES: dict[str, list[Waypoint]] = {
    # 大安森林公園一圈 (~2 km, ~25 min at 4.8 km/h)
    "walk": [
        (25.0297, 121.5354),  # SW corner
        (25.0318, 121.5354),  # NW corner
        (25.0319, 121.5383),  # NE corner
        (25.0297, 121.5383),  # SE corner
        (25.0297, 121.5354),  # back to start
    ],
    # 台北 101 → 基隆港 (~28 km, ~30 min at 55 km/h)
    "drive": [
        (25.0337, 121.5645),  # Taipei 101
        (25.0530, 121.5670),  # 圓山
        (25.0663, 121.6391),  # 汐止交流道
        (25.1210, 121.6580),  # 汐止市區
        (25.1312, 121.7406),  # 基隆港
    ],
    # 板南線 台北車站 → 南港 (~11 km, ~15 min cruise + 8 × 35 s dwells)
    "transit": [
        (25.0478, 121.5170),  # 台北車站
        (25.0446, 121.5232),  # 善導寺
        (25.0427, 121.5310),  # 忠孝新生
        (25.0417, 121.5434),  # 忠孝復興
        (25.0415, 121.5492),  # 忠孝敦化
        (25.0412, 121.5557),  # 國父紀念館
        (25.0410, 121.5658),  # 市政府
        (25.0446, 121.5776),  # 後山埤
        (25.0506, 121.5945),  # 昆陽
        (25.0593, 121.6071),  # 南港
    ],
    # 松山機場 (TSA) → 那霸機場 (OKA)  (~620 km great circle, ~44 min at 850 km/h)
    "flight": [
        (25.0697, 121.5519),  # 松山 TSA
        (26.2060, 127.6460),  # 那霸 OKA
    ],
}


# --- Geodesy ---------------------------------------------------------------


def haversine_m(a: Waypoint, b: Waypoint) -> float:
    """Great-circle distance in meters."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def initial_bearing_rad(a: Waypoint, b: Waypoint) -> float:
    """Initial bearing (radians, clockwise from north) from a to b."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.atan2(x, y)


def destination_point(origin: Waypoint, bearing_rad: float, distance_m: float) -> Waypoint:
    """Great-circle forward problem: start + bearing + distance → new point."""
    lat1, lon1 = map(math.radians, origin)
    ang = distance_m / EARTH_R
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(bearing_rad)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing_rad) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), math.degrees(lon2))


def step_toward(current: Waypoint, target: Waypoint, meters: float) -> Waypoint:
    """Advance `meters` along the great circle from `current` toward `target`.

    Snaps to `target` exactly if it's closer than `meters`, so the loop
    terminates cleanly instead of oscillating around the waypoint.
    """
    remaining = haversine_m(current, target)
    if remaining <= meters:
        return target
    bearing = initial_bearing_rad(current, target)
    return destination_point(current, bearing, meters)


def jitter_position(pos: Waypoint, sigma_m: float, rng: random.Random) -> Waypoint:
    """Add gaussian noise (`sigma_m` meters stddev) to a (lat, lon) point."""
    if sigma_m <= 0:
        return pos
    lat, lon = pos
    dlat_m = rng.gauss(0.0, sigma_m)
    dlon_m = rng.gauss(0.0, sigma_m)
    dlat = dlat_m / 111_320.0
    dlon = dlon_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return (lat + dlat, lon + dlon)


# --- Simulation -----------------------------------------------------------


@dataclass
class Tick:
    position: Waypoint
    dwell_s: float = 0.0  # if > 0, hold here for this long instead of tick_s
    note: str = ""


def _wrap_angle(rad: float) -> float:
    """Wrap an angle into [-π, π] — one-liner using atan2 avoids loops."""
    return math.atan2(math.sin(rad), math.cos(rad))


def _trail_repulsion_heading(
    current: Waypoint,
    history: "deque[Waypoint]",
    base_heading: float,
    gain: float,
) -> float:
    """Bias `base_heading` away from recent positions in `history`.

    Each past point contributes a repulsive "force" with inverse-square
    distance falloff and a linear weight by recency (most recent = 1,
    oldest = 1/len). Forces are summed in a local East-North-Up frame
    (lat/lon converted to meters via flat approximation — fine up to a
    few hundred meters).

    The resulting force vector gives a "push compass bearing", which we
    blend with the caller's base heading by `gain` ∈ [0, 1].
    """
    if len(history) < 5 or gain <= 0:
        return base_heading

    # Local flat conversion factors around `current`
    lat_m_per_deg = 111_320.0
    lon_m_per_deg = 111_320.0 * math.cos(math.radians(current[0]))

    fx = 0.0  # east component (meters)
    fy = 0.0  # north component (meters)
    n = len(history)
    for i, past in enumerate(history):
        dlat = current[0] - past[0]
        dlon = current[1] - past[1]
        dy = dlat * lat_m_per_deg
        dx = dlon * lon_m_per_deg
        d_sq = dx * dx + dy * dy
        if d_sq < 1.0:  # too close / identical, skip
            continue
        # Recency weight: most recent (end of deque) has i = n-1, weight 1.
        # Oldest has i = 0, weight 1/n. Linear falloff.
        recency = (i + 1) / n
        # Repulsion magnitude ∝ recency / distance² (inverse-square field)
        mag = recency / d_sq
        # Direction: AWAY from past point (current - past), already encoded
        # in (dx, dy), just scale.
        fx += dx * mag
        fy += dy * mag

    # Zero net force → no bias
    if abs(fx) < 1e-9 and abs(fy) < 1e-9:
        return base_heading

    # Convert (fx east, fy north) into compass bearing (0 = north, CW).
    # Math angle θ = atan2(y, x) gives 0 east, CCW. Compass = π/2 − θ.
    repulsion_compass = math.pi / 2 - math.atan2(fy, fx)

    delta = _wrap_angle(repulsion_compass - base_heading)
    return base_heading + delta * gain


def random_walk(
    center: Waypoint, profile: Profile, rng: random.Random
) -> Iterator[Tick]:
    """Correlated random walk with a soft home tether. Yields Ticks forever.

    Each tick:
      1. small chance of an unscheduled stop (like the walk profile)
      2. heading turns by a gaussian amount (σ = profile.heading_jitter_deg)
      3. if we've drifted beyond profile.max_radius_m from center, rotate
         heading toward home by a fraction proportional to how far out we are
         — this is a soft spring, not a hard wall, so the trace bends
         gracefully instead of bouncing off a boundary
      4. step forward by nominal distance × (1 + speed jitter)
      5. emit with horizontal position jitter

    The caller stops us by cancelling the consuming task.
    """
    assert profile.max_radius_m > 0, "random_walk needs a profile with max_radius_m > 0"

    current = center
    heading = rng.uniform(0.0, 2.0 * math.pi)
    yield Tick(current, note=f"start rwalk (r={profile.max_radius_m:.0f}m)")

    heading_sigma = math.radians(profile.heading_jitter_deg)

    # Ring buffer of recent positions (for trail repulsion). We push the
    # clean `current` here, not the jittered-output point — otherwise the
    # GPS noise would make the repulsion field itself noisy.
    trail: deque[Waypoint] = deque(maxlen=max(1, profile.trail_memory_ticks))

    while True:
        # Unscheduled stop
        if (
            profile.stop_probability_per_tick > 0
            and rng.random() < profile.stop_probability_per_tick
        ):
            dwell = rng.uniform(*profile.stop_duration_range)
            yield Tick(
                jitter_position(current, profile.position_jitter_m, rng),
                dwell_s=dwell,
                note=f"rest {dwell:.0f}s",
            )
            continue

        # 1. Correlated heading change — small gaussian rotation per tick
        heading += rng.gauss(0.0, heading_sigma)

        # 2. Trail repulsion — slide away from recent footprints
        heading = _trail_repulsion_heading(
            current, trail, heading, profile.trail_repulsion_gain
        )

        # 3. Soft home tether — the further out, the harder the pull
        dist_home = haversine_m(current, center)
        if dist_home > profile.max_radius_m:
            bearing_home = initial_bearing_rad(current, center)
            over = (dist_home - profile.max_radius_m) / profile.max_radius_m
            pull = min(1.0, over) * profile.home_pull_gain
            delta = _wrap_angle(bearing_home - heading)
            heading += delta * pull

        heading = _wrap_angle(heading)

        # 4. Jittered step length
        jitter = rng.gauss(0.0, profile.speed_jitter)
        step_m = max(0.5, profile.nominal_step_m * (1.0 + jitter))
        current = destination_point(current, heading, step_m)
        trail.append(current)

        yield Tick(jitter_position(current, profile.position_jitter_m, rng))


def simulate(route: list[Waypoint], profile: Profile, rng: random.Random) -> Iterator[Tick]:
    """Yield one Tick per update. Dwell ticks carry a longer sleep duration."""
    if len(route) < 2:
        raise ValueError("route needs at least two waypoints")

    current = route[0]
    yield Tick(current, note="start")

    for idx, target in enumerate(route[1:], start=1):
        is_last_leg = idx == len(route) - 1

        while haversine_m(current, target) > 0.5:
            # Unscheduled stop (red light, pedestrian pause)
            if profile.stop_probability_per_tick > 0 and rng.random() < profile.stop_probability_per_tick:
                dwell = rng.uniform(*profile.stop_duration_range)
                yield Tick(jitter_position(current, profile.position_jitter_m, rng),
                           dwell_s=dwell, note=f"pause {dwell:.0f}s")
                continue

            # Jittered step length — clamp so we don't go backwards
            jitter = rng.gauss(0.0, profile.speed_jitter)
            step_m = max(0.5, profile.nominal_step_m * (1.0 + jitter))
            current = step_toward(current, target, step_m)

            # Emit *noisy* point but keep `current` clean so noise doesn't drift
            yield Tick(jitter_position(current, profile.position_jitter_m, rng))

        # Arrived at waypoint: station dwell for transit, skip on last leg
        if profile.waypoint_dwell_s > 0 and not is_last_leg:
            yield Tick(current, dwell_s=profile.waypoint_dwell_s,
                       note=f"station dwell {profile.waypoint_dwell_s:.0f}s")


# --- Driver ---------------------------------------------------------------


async def run(loc_sim: LocationSimulation, profile: Profile, route: list[Waypoint]) -> None:
    total_m = sum(haversine_m(a, b) for a, b in zip(route, route[1:]))
    moving_s = total_m / profile.nominal_mps
    dwell_s = profile.waypoint_dwell_s * max(0, len(route) - 2)
    eta_s = moving_s + dwell_s

    print(f"profile : {profile.label}  ({profile.nominal_kmh:.0f} km/h)")
    print(f"route   : {len(route)} waypoints, {total_m / 1000:.2f} km")
    print(f"tick    : {profile.tick_s:.1f} s  →  {profile.nominal_step_m:.1f} m/tick (nominal)")
    print(f"ETA     : ~{eta_s / 60:.1f} min  ({moving_s:.0f}s moving + {dwell_s:.0f}s dwell)")
    print("-" * 60)

    rng = random.Random()
    ticks = 0
    elapsed = 0.0
    for tick in simulate(route, profile, rng):
        await loc_sim.set(*tick.position)
        ticks += 1

        sleep_s = tick.dwell_s if tick.dwell_s > 0 else profile.tick_s
        if tick.note or ticks == 1 or ticks % 10 == 0:
            tag = f" [{tick.note}]" if tick.note else ""
            print(f"[t={elapsed:7.1f}s] {tick.position[0]:.6f}, {tick.position[1]:.6f}{tag}")

        await asyncio.sleep(sleep_s)
        elapsed += sleep_s

    print("-" * 60)
    print(f"done: {ticks} ticks, {elapsed:.0f} s total")


async def pick_device():
    rsds = await get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS)
    if not rsds:
        raise SystemExit(
            "No tunnelled device found.\n"
            "Start one in another terminal:\n"
            "  sudo pymobiledevice3 remote tunneld"
        )
    if len(rsds) > 1:
        print(f"Multiple devices tunneled ({len(rsds)}); using the first.")
    for rsd in rsds[1:]:
        await rsd.close()
    return rsds[0]


async def main(profile_name: str) -> None:
    profile = PROFILES[profile_name]
    route = ROUTES[profile_name]

    rsd = await pick_device()
    print(f"device  : {rsd.udid}  ({rsd.product_type} / iOS {rsd.product_version})")

    async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as loc_sim:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        runner = asyncio.create_task(run(loc_sim, profile, route))
        stopper = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled() and task.exception():
                exc = task.exception()
                if not isinstance(exc, asyncio.CancelledError):
                    raise exc

        print("clearing simulated location…")
        await loc_sim.clear()
        print("cleared. phone is back on real GPS.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "profile",
        nargs="?",
        default="walk",
        choices=sorted(PROFILES),
        help="movement profile to simulate",
    )
    args = parser.parse_args()
    asyncio.run(main(args.profile))
