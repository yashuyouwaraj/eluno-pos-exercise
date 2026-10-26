import unittest
from datetime import date

from eluno_pos.business_time import bengaluru_business_date, bengaluru_today


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


if __name__ == "__main__":
    unittest.main()
