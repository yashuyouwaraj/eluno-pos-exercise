import unittest
from datetime import date, datetime, timezone
import os

from unittest.mock import patch

from eluno_pos.business_time import bengaluru_business_date, bengaluru_today, business_timezone_name


class BengaluruBusinessTimeTests(unittest.TestCase):
    def test_utc_timestamp_boundary_maps_to_bengaluru_calendar_date(self):
        self.assertEqual(
            date(2026, 10, 26),
            bengaluru_business_date("2026-10-26T18:29:59Z"),
        )
        self.assertEqual(
            date(2026, 10, 27),
            bengaluru_business_date("2026-10-26T18:30:00Z"),
        )

    def test_naive_timestamps_are_interpreted_as_bengaluru_local_time(self):
        self.assertEqual(
            date(2026, 10, 27),
            bengaluru_business_date("2026-10-27T00:05:00"),
        )

    def test_business_today_is_always_a_date(self):
        self.assertIsInstance(bengaluru_today(), date)

    def test_business_today_rolls_over_at_bengaluru_midnight(self):
        class FixedDateTime:
            current = datetime(2026, 10, 29, 18, 29, 59, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current

        with patch("eluno_pos.business_time.datetime", FixedDateTime):
            self.assertEqual(date(2026, 10, 29), bengaluru_today())
            FixedDateTime.current = datetime(2026, 10, 29, 18, 30, 0, tzinfo=timezone.utc)
            self.assertEqual(date(2026, 10, 30), bengaluru_today())

    def test_configured_timezone_controls_operational_date(self):
        with patch.dict(os.environ, {"ELUNO_BUSINESS_TIMEZONE": "UTC"}):
            self.assertEqual("UTC", business_timezone_name())
            self.assertEqual(date(2026, 10, 26), bengaluru_business_date("2026-10-26T23:59:59Z"))
            self.assertEqual(date(2026, 10, 27), bengaluru_business_date("2026-10-27T00:00:00Z"))

    def test_invalid_configured_timezone_is_reported(self):
        with patch.dict(os.environ, {"ELUNO_BUSINESS_TIMEZONE": "Not/A-Timezone"}):
            with self.assertRaisesRegex(ValueError, "ELUNO_BUSINESS_TIMEZONE"):
                bengaluru_today()


if __name__ == "__main__":
    unittest.main()
