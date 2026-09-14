"""Run with plain Python in the installed runtime image, without optional extras."""
from datetime import date, datetime, timezone
import unittest

from ai4ia_api.workflows.scheduling import ScheduleRule, occurrence, require_timezone_data


class SystemTimezoneDataTests(unittest.TestCase):
    def test_real_system_timezone_rules_cover_supported_calendar_edges(self):
        require_timezone_data()
        cases = (
            ("UTC", "02:30", date(2026, 3, 8), datetime(2026, 3, 8, 2, 30, tzinfo=timezone.utc)),
            ("America/New_York", "02:30", date(2026, 3, 8), None),
            ("America/New_York", "01:30", date(2026, 11, 1), datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)),
            ("Australia/Lord_Howe", "02:15", date(2026, 10, 4), None),
            ("Pacific/Apia", "12:00", date(2011, 12, 30), None),
        )
        for zone, clock, day, expected in cases:
            with self.subTest(zone=zone, day=day):
                rule = ScheduleRule(
                    frequency="daily", timezone=zone, localTime=clock, maxOccurrences=2,
                )
                result = occurrence(rule, day)
                self.assertEqual(result.dueAt if result else None, expected)
                if result:
                    self.assertEqual(result.zoneVersion, "system-tzif")
                    self.assertEqual(len(result.zoneDigest), 64)


if __name__ == "__main__":
    unittest.main()
