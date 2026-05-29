import math
from pikmin_walk import figure_points, haversine_m

CENTER = (43.0686, 141.3507)  # 札幌駅

def _dist(p):
    return haversine_m(CENTER, p)

def test_figure_points_closed_loop():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    assert len(pts) > 50
    assert haversine_m(pts[0], pts[-1]) < 0.5

def test_figure_points_radii():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    dists = [_dist(p) for p in pts]
    assert 93.0 <= max(dists) <= 97.0
    assert any(abs(d - 70.0) < 1.5 for d in dists)
    assert abs(_dist(pts[0]) - 70.0) < 1.0

def test_figure_points_continuous():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    gaps = [haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    assert max(gaps) <= 4.5
