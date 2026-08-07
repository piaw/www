import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import garmin_coaching as coaching


TZ = "America/Los_Angeles"


class FakeFetch:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, path, params=None):
        copied = dict(params or {})
        self.calls.append((path, copied))
        key = (path, copied.get("oldest"), copied.get("newest"))
        if key not in self.responses:
            raise AssertionError(f"Unexpected fetch call: {key}")
        return self.responses[key]


def wellness_records():
    start = date(2026, 8, 1)
    records = []
    for offset in range(7):
        current = start + timedelta(days=offset)
        records.append(
            {
                "id": current.isoformat(),
                "hrv": 50 + offset,
                "hrvSDNN": 70 + offset,
                "restingHR": 45 + (offset % 2),
                "avgSleepingHR": 42 + (offset % 2),
                "sleepSecs": 8 * 3600,
                "sleepScore": 82 + offset,
                "weight": 70.0 + (offset / 10),
            }
        )
    records[-1]["ctl"] = 69
    records[-1]["atl"] = 12
    return records


class GarminCoachingTest(unittest.TestCase):
    def test_normal_wellness_and_activity_data(self):
        activities = [
            {
                "start_date_local": "2026-08-03T08:00:00",
                "type": "Ride",
                "moving_time": 1800,
            },
            {
                "start_date_local": "2026-08-05T08:00:00",
                "type": "Ride",
                "moving_time": 3600,
            },
        ]
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
                ("activities", "2026-08-01", "2026-08-07"): activities,
            }
        )

        brief, context = coaching.generate_brief(
            fetcher=fake,
            report_date=date(2026, 8, 7),
            timezone_name=TZ,
        )

        self.assertEqual(context.oldest, "2026-08-01")
        self.assertEqual(context.newest, "2026-08-07")
        self.assertTrue(context.readiness_reliable)
        self.assertEqual(context.latest_wellness_date, "2026-08-07")
        self.assertEqual(context.latest_activity_date, "2026-08-05")
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("Activities:         2 (2x Ride)", brief)
        self.assertIn("Latest wellness date: 2026-08-07", brief)
        self.assertIn("Latest activity date: 2026-08-05", brief)
        self.assertIn("Fresh / race-ready", brief)

    def test_empty_seven_day_activity_data_with_older_activity_found(self):
        older_activities = [
            {
                "start_date_local": "2026-07-31T08:00:00",
                "type": "Ride",
                "moving_time": 3600,
            }
        ]
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
                ("activities", "2026-08-01", "2026-08-07"): [],
                ("activities", "2026-07-02", "2026-07-31"): older_activities,
            }
        )

        brief, context = coaching.generate_brief(
            fetcher=fake,
            report_date=date(2026, 8, 7),
            timezone_name=TZ,
        )

        self.assertFalse(context.readiness_reliable)
        self.assertEqual(context.latest_wellness_date, "2026-08-07")
        self.assertEqual(context.latest_activity_date, "2026-07-31")
        self.assertEqual(context.probe_oldest, "2026-07-02")
        self.assertEqual(context.probe_newest, "2026-07-31")
        self.assertIn("ACTIVITY SYNC / DATA QUALITY WARNING", brief)
        self.assertIn("Most recent activity found during probe: 2026-07-31", brief)
        self.assertIn("ATL, CTL, and TSB readiness are unreliable", brief)
        self.assertIn("Readiness unknown", brief)
        self.assertNotIn("Fresh / race-ready", brief)

    def test_completely_empty_activity_history(self):
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
                ("activities", "2026-08-01", "2026-08-07"): [],
                ("activities", "2026-07-02", "2026-07-31"): [],
            }
        )

        brief, context = coaching.generate_brief(
            fetcher=fake,
            report_date=date(2026, 8, 7),
            timezone_name=TZ,
        )

        self.assertFalse(context.readiness_reliable)
        self.assertIsNone(context.latest_activity_date)
        self.assertIn("Latest activity date: unknown", brief)
        self.assertIn(
            "No activities found in the current window or preceding 30 days.",
            brief,
        )
        self.assertNotIn("Fresh / race-ready", brief)

    def test_malformed_activity_api_response(self):
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
                ("activities", "2026-08-01", "2026-08-07"): {"items": []},
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "activities endpoint returned dict; expected a list",
        ):
            coaching.generate_brief(
                fetcher=fake,
                report_date=date(2026, 8, 7),
                timezone_name=TZ,
            )

    def test_america_los_angeles_date_boundaries(self):
        before_midnight_la = datetime(2026, 8, 8, 6, 30, tzinfo=timezone.utc)
        after_midnight_la = datetime(2026, 8, 8, 7, 30, tzinfo=timezone.utc)

        self.assertEqual(
            coaching.date_range(
                coaching.DAYS_BACK,
                now=before_midnight_la,
                timezone_name=TZ,
            ),
            ("2026-08-01", "2026-08-07"),
        )
        self.assertEqual(
            coaching.date_range(
                coaching.DAYS_BACK,
                now=after_midnight_la,
                timezone_name=TZ,
            ),
            ("2026-08-02", "2026-08-08"),
        )

        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
                ("activities", "2026-08-01", "2026-08-07"): [
                    {
                        "start_date": "2026-08-08T06:15:00Z",
                        "type": "Ride",
                        "moving_time": 600,
                    }
                ],
            }
        )
        _brief, context = coaching.generate_brief(
            fetcher=fake,
            now=before_midnight_la,
            timezone_name=TZ,
        )

        self.assertEqual(context.report_date, date(2026, 8, 7))
        self.assertEqual(context.oldest, "2026-08-01")
        self.assertEqual(context.newest, "2026-08-07")
        self.assertEqual(context.latest_activity_date, "2026-08-07")


if __name__ == "__main__":
    unittest.main()
