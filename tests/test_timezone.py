import unittest

from server import _set_and_verify_timezone, _timezone_applied


class FakeLockdown:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.current = self.snapshots.pop(0)
        self.set_calls = []

    async def get_value(self, *, key):
        value = self.current[0] if key == "TimeZone" else self.current[1]
        if key == "TimeZoneOffsetFromUTC" and self.snapshots:
            self.current = self.snapshots.pop(0)
        return value

    async def set_timezone(self, timezone_name):
        self.set_calls.append(timezone_name)


class TimezoneVerificationTests(unittest.TestCase):
    def test_accepts_matching_timezone_and_offset(self):
        self.assertTrue(_timezone_applied("Asia/Taipei", 28800, "Asia/Taipei", 28800.0))

    def test_rejects_setvalue_echo_when_device_keeps_old_timezone(self):
        self.assertFalse(
            _timezone_applied("Asia/Taipei", 28800, "America/St_Johns", -9000.0)
        )

    def test_rejects_stale_offset_even_when_name_matches(self):
        self.assertFalse(_timezone_applied("Asia/Taipei", 28800, "Asia/Taipei", 32400))

    def test_rejects_missing_offset(self):
        self.assertFalse(_timezone_applied("Asia/Taipei", 28800, "Asia/Taipei", None))


class TimezoneWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_device_state_instead_of_trusting_set_response(self):
        lockdown = FakeLockdown([
            ("America/St_Johns", -9000),
            ("America/St_Johns", -9000),
            ("Asia/Taipei", 28800),
        ])

        initial, actual, offset = await _set_and_verify_timezone(
            lockdown, "Asia/Taipei", 28800, verify_delays=(0.0,),
        )

        self.assertEqual(initial, "America/St_Johns")
        self.assertEqual((actual, offset), ("Asia/Taipei", 28800))
        self.assertEqual(lockdown.set_calls, ["Asia/Taipei"])

    async def test_reports_unchanged_device_after_verification_window(self):
        lockdown = FakeLockdown([("America/St_Johns", -9000)])

        initial, actual, offset = await _set_and_verify_timezone(
            lockdown, "Asia/Taipei", 28800, verify_delays=(0.0, 0.0),
        )

        self.assertEqual(initial, "America/St_Johns")
        self.assertEqual((actual, offset), ("America/St_Johns", -9000))
        self.assertEqual(lockdown.set_calls, ["Asia/Taipei"])


if __name__ == "__main__":
    unittest.main()
