from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schedule_upload as schedule


class ScheduleTests(unittest.TestCase):
    def test_weekday_boundaries(self):
        for time, expected in [('06:59:59', True), ('07:00:00', False),
                               ('19:59:59', False), ('20:00:00', True)]:
            now = datetime.fromisoformat('2026-10-27T' + time).replace(tzinfo=ZoneInfo('Australia/Melbourne'))
            self.assertEqual(schedule.allowed(now, schedule.DEFAULT_SCHEDULE), expected)

    def test_weekend_including_sunday_to_monday(self):
        for date, expected in [('2026-10-30T20:00', True), ('2026-10-31T12:00', True),
                               ('2026-11-01T12:00', True), ('2026-11-02T06:59', True),
                               ('2026-11-02T07:00', False)]:
            now = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('Australia/Melbourne'))
            self.assertEqual(schedule.allowed(now, schedule.DEFAULT_SCHEDULE), expected)

    def test_next_transition_skips_weekend_and_observes_timezone(self):
        for date in ['2026-10-30T21:00', '2026-10-31T12:00']:
            now = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('Australia/Melbourne'))
            self.assertEqual(schedule.next_transition(now, schedule.DEFAULT_SCHEDULE).isoformat(), '2026-11-02T07:00:00+11:00')
        utc = datetime.fromisoformat('2026-10-26T20:00:00+00:00')
        self.assertFalse(schedule.allowed(utc, schedule.DEFAULT_SCHEDULE))

    def test_daylight_saving_does_not_change_local_boundary(self):
        now = datetime.fromisoformat('2027-04-03T21:00').replace(tzinfo=ZoneInfo('Australia/Melbourne'))
        self.assertEqual(schedule.next_transition(now, schedule.DEFAULT_SCHEDULE).isoformat(), '2027-04-05T07:00:00+10:00')
