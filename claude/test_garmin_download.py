import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import garmin_download as gd


def activity(activity_id, start_time, name="Morning Ride"):
    return {
        "activityId": activity_id,
        "activityName": name,
        "activityType": {"typeKey": "cycling"},
        "startTimeGMT": start_time,
        "startTimeLocal": start_time.replace("Z", ""),
        "duration": 3600,
        "distance": 25000,
        "averageHR": 140,
    }


class FakeGarmin:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_activities(self, start, limit):
        self.calls.append(("get_activities", start, limit))
        return self.pages.get(start, [])

    def get_activity_details(self, activity_id):
        return {
            "summaryDTO": {
                "duration": 3600,
                "distance": 25000,
                "elevationGain": 500,
            },
            "metricDescriptors": [
                {"key": "directTimestamp"},
                {"key": "heartRate"},
                {"key": "power"},
            ],
            "activityDetailMetrics": [
                {
                    "metrics": [
                        {"metricsIndex": 2, "value": 180},
                        {"metricsIndex": 0, "value": 0},
                        {"metricsIndex": 1, "value": 130},
                    ]
                },
                {
                    "metrics": [
                        {"metricsIndex": 2, "value": 190},
                        {"metricsIndex": 0, "value": 1},
                        {"metricsIndex": 1, "value": 135},
                    ]
                },
            ],
        }

    def get_activity_splits(self, activity_id):
        return [{"activityId": activity_id, "splitNumber": 1}]


class FakeGarth:
    def __init__(self):
        self.loaded = None
        self.login_args = None
        self.dumped = None
        self.profile = {
            "displayName": "Rider",
            "fullName": "Test Rider",
        }

    def load(self, tokenstore):
        self.loaded = tokenstore

    def login(self, email, password, prompt_mfa=None):
        self.login_args = (email, password, prompt_mfa())

    def dump(self, tokenstore):
        self.dumped = tokenstore

    def connectapi(self, path):
        return {"userData": {"measurementSystem": "metric"}}


class FakeGarminClient:
    def __init__(self):
        self.garth = FakeGarth()
        self.display_name = None
        self.full_name = None
        self.unit_system = None


class GarminDownloadTest(unittest.TestCase):
    def test_fetch_recent_activities_stops_at_cutoff(self):
        api = FakeGarmin(
            {
                0: [
                    activity(101, "2026-08-07T15:00:00Z"),
                    activity(100, "2026-08-01T15:00:00Z"),
                ],
                2: [activity(99, "2026-07-31T15:00:00Z")],
            }
        )
        cutoff = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)

        activities = gd.fetch_recent_activities(api, cutoff, page_limit=2, max_pages=3)

        self.assertEqual([gd.activity_id(item) for item in activities], ["101", "100"])
        self.assertEqual(api.calls, [("get_activities", 0, 2), ("get_activities", 2, 2)])

    def test_fetch_recent_activities_rejects_malformed_response(self):
        api = FakeGarmin({0: {"items": []}})

        with self.assertRaisesRegex(ValueError, "expected a list"):
            gd.fetch_recent_activities(
                api,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                page_limit=100,
                max_pages=1,
            )

    def test_write_activity_file_normalizes_garmin_payload(self):
        api = FakeGarmin({})
        with tempfile.TemporaryDirectory() as tmp:
            path = gd.write_activity_file(
                api,
                activity(101, "2026-08-07T15:00:00Z", name="Lunch / Ride"),
                data_dir=Path(tmp),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn("2026-08-07_1500_101_Lunch_Ride.json", str(path))
        self.assertEqual(payload["source"], "garmin")
        self.assertEqual(payload["activity"]["id"], "101")
        self.assertEqual(payload["activity"]["type"], "cycling")
        self.assertEqual(payload["activity"]["total_elevation_gain"], 500)
        self.assertEqual(payload["streams"]["heartrate"], [130, 135])
        self.assertEqual(payload["streams"]["power"], [180, 190])
        self.assertEqual(payload["garmin"]["splits"][0]["splitNumber"], 1)

    def test_existing_activity_ids_detects_saved_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-08-07_1500_101_Morning_Ride.json").write_text("{}")
            (root / ".garmin_last_fetch.json").write_text("{}")

            self.assertEqual(gd.existing_activity_ids(root), {"101"})

    def test_state_cutoff_uses_overlap_after_last_activity(self):
        state = {"last_activity_start_utc": "2026-08-07T15:00:00Z"}

        cutoff = gd.cutoff_from_state(state, lookback_days=30, overlap_days=3)

        self.assertEqual(cutoff.isoformat(), "2026-08-04T15:00:00+00:00")

    def test_save_state_tracks_latest_downloaded_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".garmin_last_fetch.json"
            gd.save_state(
                {},
                [
                    activity(101, "2026-08-07T15:00:00Z"),
                    activity(102, "2026-08-08T15:00:00Z"),
                ],
                path=path,
            )
            state = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(state["last_activity_start_utc"], "2026-08-08T15:00:00Z")
        self.assertIn("last_success_utc", state)

    def test_login_uses_existing_tokenstore_without_prompting_mfa(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "oauth1_token.json").write_text("{}", encoding="utf-8")
            Path(tmp, "oauth2_token.json").write_text("{}", encoding="utf-8")
            api = FakeGarminClient()

            gd.login_garmin_client(
                api,
                "rider@example.com",
                "secret",
                tmp,
                prompt_mfa=lambda: "123456",
            )

        self.assertEqual(api.garth.loaded, tmp)
        self.assertIsNone(api.garth.login_args)
        self.assertIsNone(api.garth.dumped)
        self.assertEqual(api.display_name, "Rider")
        self.assertEqual(api.unit_system, "metric")

    def test_login_prompts_for_mfa_and_dumps_new_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeGarminClient()

            gd.login_garmin_client(
                api,
                "rider@example.com",
                "secret",
                tmp,
                prompt_mfa=lambda: "123456",
            )

        self.assertIsNone(api.garth.loaded)
        self.assertEqual(api.garth.login_args, ("rider@example.com", "secret", "123456"))
        self.assertEqual(api.garth.dumped, tmp)
        self.assertEqual(api.full_name, "Test Rider")

    def test_rate_limit_error_message_names_wait_and_token_cache(self):
        message = gd.garmin_login_error_message(
            Exception("Mobile login returned 429 — IP rate limited by Garmin")
        )

        self.assertIn("rate-limiting", message)
        self.assertIn("Wait a while", message)
        self.assertIn("token cache", message)


if __name__ == "__main__":
    unittest.main()
