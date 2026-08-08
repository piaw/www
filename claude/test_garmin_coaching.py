import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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


class FakeActivityLoader:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, oldest, newest, timezone_name):
        self.calls.append((oldest, newest, timezone_name))
        key = (oldest, newest)
        if key not in self.responses:
            raise AssertionError(f"Unexpected activity load: {key}")
        return self.responses[key]


def garmin_activity(start_time, activity_type="road_biking", moving_time=1800):
    return {
        "source": "garmin",
        "activity": {
            "source": "garmin",
            "start_date_local": start_time,
            "type": activity_type,
            "moving_time": moving_time,
        },
    }


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
            garmin_activity("2026-08-03T08:00:00", moving_time=1800),
            garmin_activity("2026-08-05T08:00:00", moving_time=3600),
        ]
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
            }
        )
        activity_loader = FakeActivityLoader(
            {("2026-08-01", "2026-08-07"): activities}
        )

        brief, context = coaching.generate_brief(
            fetcher=fake,
            activity_loader=activity_loader,
            report_date=date(2026, 8, 7),
            timezone_name=TZ,
        )

        self.assertEqual(context.oldest, "2026-08-01")
        self.assertEqual(context.newest, "2026-08-07")
        self.assertTrue(context.readiness_reliable)
        self.assertEqual(context.latest_wellness_date, "2026-08-07")
        self.assertEqual(context.latest_activity_date, "2026-08-05")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(activity_loader.calls, [("2026-08-01", "2026-08-07", TZ)])
        self.assertIn("Activities:         2 (2x road_biking)", brief)
        self.assertIn("Latest wellness date: 2026-08-07", brief)
        self.assertIn("Latest activity date: 2026-08-05", brief)
        self.assertIn("Activity source: Local Garmin activity files", brief)
        self.assertIn("Fresh / race-ready", brief)

    def test_empty_seven_day_activity_data_with_older_activity_found(self):
        older_activities = [garmin_activity("2026-07-31T08:00:00", moving_time=3600)]
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
            }
        )
        activity_loader = FakeActivityLoader(
            {
                ("2026-08-01", "2026-08-07"): [],
                ("2026-07-02", "2026-07-31"): older_activities,
            }
        )

        brief, context = coaching.generate_brief(
            fetcher=fake,
            activity_loader=activity_loader,
            report_date=date(2026, 8, 7),
            timezone_name=TZ,
        )

        self.assertFalse(context.readiness_reliable)
        self.assertEqual(context.latest_wellness_date, "2026-08-07")
        self.assertEqual(context.latest_activity_date, "2026-07-31")
        self.assertEqual(context.probe_oldest, "2026-07-02")
        self.assertEqual(context.probe_newest, "2026-07-31")
        self.assertEqual(
            activity_loader.calls,
            [
                ("2026-08-01", "2026-08-07", TZ),
                ("2026-07-02", "2026-07-31", TZ),
            ],
        )
        self.assertIn("ACTIVITY SYNC / DATA QUALITY WARNING", brief)
        self.assertIn(
            "Local Garmin activity files contain 0 activities for 2026-08-01",
            brief,
        )
        self.assertIn("Most recent activity found during probe: 2026-07-31", brief)
        self.assertIn("ATL, CTL, and TSB readiness are unreliable", brief)
        self.assertIn("Readiness unknown", brief)
        self.assertNotIn("Fresh / race-ready", brief)

    def test_completely_empty_activity_history(self):
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
            }
        )
        activity_loader = FakeActivityLoader(
            {
                ("2026-08-01", "2026-08-07"): [],
                ("2026-07-02", "2026-07-31"): [],
            }
        )

        brief, context = coaching.generate_brief(
            fetcher=fake,
            activity_loader=activity_loader,
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

    def test_malformed_activity_loader_response(self):
        fake = FakeFetch(
            {
                ("wellness", "2026-08-01", "2026-08-07"): wellness_records(),
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "Garmin activity loader returned dict; expected a list",
        ):
            coaching.generate_brief(
                fetcher=fake,
                activity_loader=lambda _oldest, _newest, _tz: {"items": []},
                report_date=date(2026, 8, 7),
                timezone_name=TZ,
            )

    def test_load_garmin_activities_filters_files_by_source_and_local_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "garmin_la_aug7.json").write_text(
                json.dumps(
                    {
                        "source": "garmin",
                        "activity": {
                            "source": "garmin",
                            "start_date": "2026-08-08T06:15:00Z",
                            "type": "road_biking",
                            "moving_time": 600,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "garmin_la_aug8.json").write_text(
                json.dumps(
                    {
                        "source": "garmin",
                        "activity": {
                            "source": "garmin",
                            "start_date": "2026-08-08T07:15:00Z",
                            "type": "road_biking",
                            "moving_time": 600,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "legacy_strava.json").write_text(
                json.dumps(
                    {
                        "activity": {
                            "start_date_local": "2026-08-07T08:00:00Z",
                            "type": "Ride",
                            "moving_time": 600,
                        }
                    }
                ),
                encoding="utf-8",
            )

            activities = coaching.load_garmin_activities(
                "2026-08-01",
                "2026-08-07",
                TZ,
                data_dir=root,
            )

        self.assertEqual(len(activities), 1)
        self.assertEqual(
            coaching.activity_field(activities[0], "start_date"),
            "2026-08-08T06:15:00Z",
        )

    def test_malformed_garmin_activity_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.json").write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Garmin activity file is malformed"):
                coaching.load_garmin_activities(
                    "2026-08-01",
                    "2026-08-07",
                    TZ,
                    data_dir=root,
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
            }
        )
        activity_loader = FakeActivityLoader(
            {
                ("2026-08-01", "2026-08-07"): [
                    {
                        "source": "garmin",
                        "activity": {
                            "source": "garmin",
                            "start_date": "2026-08-08T06:15:00Z",
                            "type": "road_biking",
                            "moving_time": 600,
                        },
                    }
                ],
            }
        )
        _brief, context = coaching.generate_brief(
            fetcher=fake,
            activity_loader=activity_loader,
            now=before_midnight_la,
            timezone_name=TZ,
        )

        self.assertEqual(context.report_date, date(2026, 8, 7))
        self.assertEqual(context.oldest, "2026-08-01")
        self.assertEqual(context.newest, "2026-08-07")
        self.assertEqual(context.latest_activity_date, "2026-08-07")

    def test_america_los_angeles_fallback_without_tzdata(self):
        def missing_zoneinfo(_key):
            raise coaching.ZoneInfoNotFoundError("missing tzdata")

        with mock.patch.object(coaching, "ZoneInfo", side_effect=missing_zoneinfo):
            before_midnight_la = datetime(2026, 8, 8, 6, 30, tzinfo=timezone.utc)
            after_midnight_la = datetime(2026, 8, 8, 7, 30, tzinfo=timezone.utc)
            winter_evening_la = datetime(2026, 1, 1, 7, 30, tzinfo=timezone.utc)

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
            self.assertEqual(
                coaching.local_report_date(
                    now=winter_evening_la,
                    timezone_name=TZ,
                ),
                date(2025, 12, 31),
            )
            self.assertEqual(
                coaching.parse_datetime_as_local_date(
                    "2026-08-08T06:15:00Z",
                    TZ,
                ),
                date(2026, 8, 7),
            )


if __name__ == "__main__":
    unittest.main()
