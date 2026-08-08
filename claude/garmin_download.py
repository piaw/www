#!/usr/bin/env python3
"""
Garmin Connect Activity Downloader
----------------------------------
Fetches recent activities directly from Garmin Connect and writes normalized
JSON records to data/activities/.

Required env vars:
    GARMIN_EMAIL
    GARMIN_PASSWORD

Optional env vars:
    GARMINTOKENS             Directory for garth/Garmin session tokens
    GARMIN_LOOKBACK_DAYS     Initial lookback window when no state exists
    GARMIN_OVERLAP_DAYS      Re-check overlap after the last synced activity
    GARMIN_PAGE_LIMIT        Garmin activity page size
    GARMIN_MAX_PAGES         Maximum pages to inspect per run
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "activities"
STATE_FILE = DATA_DIR / ".garmin_last_fetch.json"
SCHEMA_VERSION = 1

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_OVERLAP_DAYS = 3
DEFAULT_PAGE_LIMIT = 100
DEFAULT_MAX_PAGES = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️  Ignoring invalid {name}={raw!r}; using {default}")
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Garmin Connect activities")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=env_int("GARMIN_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS),
        help="Initial lookback window when no Garmin sync state exists",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=env_int("GARMIN_OVERLAP_DAYS", DEFAULT_OVERLAP_DAYS),
        help="Overlap to re-check before the last synced Garmin activity",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=env_int("GARMIN_PAGE_LIMIT", DEFAULT_PAGE_LIMIT),
        help="Garmin activity page size",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=env_int("GARMIN_MAX_PAGES", DEFAULT_MAX_PAGES),
        help="Maximum Garmin activity pages to inspect",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List candidate activities without writing files or state",
    )
    return parser.parse_args()


def load_state(path: Path = STATE_FILE) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Garmin state file is malformed: {path}") from exc


def save_state(
    state: Dict[str, Any],
    downloaded_activities: Iterable[Dict[str, Any]],
    path: Path = STATE_FILE,
) -> None:
    latest = latest_activity_start_utc(downloaded_activities)
    if latest is not None:
        previous = parse_datetime(state.get("last_activity_start_utc"), assume_utc=True)
        if previous is None or latest > previous:
            state["last_activity_start_utc"] = latest.isoformat().replace("+00:00", "Z")
    state["last_success_utc"] = utc_now().isoformat().replace("+00:00", "Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def cutoff_from_state(
    state: Dict[str, Any],
    lookback_days: int,
    overlap_days: int,
    now: Optional[datetime] = None,
) -> datetime:
    latest = parse_datetime(state.get("last_activity_start_utc"), assume_utc=True)
    if latest is not None:
        return latest - timedelta(days=overlap_days)
    reference = now or utc_now()
    return reference - timedelta(days=lookback_days)


def parse_datetime(value: Any, assume_utc: bool = False) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None and assume_utc:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def activity_id(activity: Dict[str, Any]) -> Optional[str]:
    for key in ("activityId", "id", "activity_id"):
        value = activity.get(key)
        if value is not None:
            return str(value)
    return None


def activity_name(activity: Dict[str, Any]) -> str:
    return str(activity.get("activityName") or activity.get("name") or "Garmin Activity")


def activity_type(activity: Dict[str, Any]) -> str:
    raw = activity.get("activityType") or activity.get("type") or {}
    if isinstance(raw, dict):
        return str(raw.get("typeKey") or raw.get("typeKeyId") or raw.get("name") or "Other")
    return str(raw or "Other")


def activity_start_utc(activity: Dict[str, Any]) -> Optional[datetime]:
    for key in ("startTimeGMT", "startTimeUTC", "startDate", "start_time_gmt"):
        parsed = parse_datetime(activity.get(key), assume_utc=True)
        if parsed is not None:
            return parsed.astimezone(timezone.utc)
    return None


def activity_start_local(activity: Dict[str, Any]) -> Optional[datetime]:
    for key in ("startTimeLocal", "startDateLocal", "start_date_local"):
        parsed = parse_datetime(activity.get(key), assume_utc=False)
        if parsed is not None:
            return parsed
    return activity_start_utc(activity)


def latest_activity_start_utc(activities: Iterable[Dict[str, Any]]) -> Optional[datetime]:
    dates = [
        parsed
        for parsed in (activity_start_utc(activity) for activity in activities)
        if parsed is not None
    ]
    return max(dates) if dates else None


def ensure_activity_list(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError(
            f"Garmin get_activities returned {type(payload).__name__}; expected a list"
        )
    return payload


def fetch_recent_activities(
    api: Any,
    cutoff: datetime,
    page_limit: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    activities: List[Dict[str, Any]] = []
    cutoff_utc = cutoff.astimezone(timezone.utc)

    for page in range(max_pages):
        start = page * page_limit
        batch = ensure_activity_list(api.get_activities(start, page_limit))
        if not batch:
            break

        saw_older_activity = False
        for item in batch:
            if not isinstance(item, dict):
                continue
            started = activity_start_utc(item)
            if started is not None and started <= cutoff_utc:
                saw_older_activity = True
                continue
            activities.append(item)

        if len(batch) < page_limit or saw_older_activity:
            break

    return activities


def safe_filename_part(value: str, max_length: int = 48) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._@+-]+", "_", value.strip())
    cleaned = cleaned.strip("._")
    return (cleaned or "Garmin_Activity")[:max_length]


def activity_filename(activity: Dict[str, Any]) -> str:
    started = activity_start_local(activity) or activity_start_utc(activity) or utc_now()
    if started.tzinfo is not None:
        started = started.astimezone(timezone.utc)
    activity_date = started.strftime("%Y-%m-%d_%H%M")
    identifier = activity_id(activity) or "unknown"
    name = safe_filename_part(activity_name(activity))
    return f"{activity_date}_{identifier}_{name}.json"


def existing_activity_ids(data_dir: Path = DATA_DIR) -> set:
    ids = set()
    if not data_dir.exists():
        return ids
    for path in data_dir.glob("*.json"):
        match = re.match(r"^\d{4}-\d{2}-\d{2}_\d{4}_([^_]+)_", path.name)
        if match:
            ids.add(match.group(1))
    return ids


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def normalized_activity(summary: Dict[str, Any], details: Dict[str, Any]) -> Dict[str, Any]:
    local_start = activity_start_local(summary)
    utc_start = activity_start_utc(summary)
    activity_summary = details.get("summaryDTO") if isinstance(details, dict) else {}
    activity_summary = activity_summary if isinstance(activity_summary, dict) else {}

    return {
        "id": activity_id(summary),
        "source": "garmin",
        "name": activity_name(summary),
        "type": activity_type(summary),
        "start_date": utc_start.isoformat().replace("+00:00", "Z") if utc_start else None,
        "start_date_local": local_start.isoformat() if local_start else None,
        "moving_time": first_present(
            summary.get("movingDuration"),
            activity_summary.get("movingDuration"),
            summary.get("duration"),
            activity_summary.get("duration"),
        ),
        "elapsed_time": first_present(
            summary.get("elapsedDuration"),
            activity_summary.get("elapsedDuration"),
            summary.get("duration"),
            activity_summary.get("duration"),
        ),
        "distance": first_present(summary.get("distance"), activity_summary.get("distance")),
        "total_elevation_gain": first_present(
            summary.get("elevationGain"),
            summary.get("elevationGainMeters"),
            activity_summary.get("elevationGain"),
        ),
        "average_heartrate": first_present(
            summary.get("averageHR"),
            summary.get("averageHeartRate"),
            activity_summary.get("averageHR"),
        ),
        "max_heartrate": first_present(
            summary.get("maxHR"),
            summary.get("maxHeartRate"),
            activity_summary.get("maxHR"),
        ),
        "average_watts": first_present(
            summary.get("avgPower"),
            summary.get("averagePower"),
            activity_summary.get("avgPower"),
        ),
        "max_watts": first_present(
            summary.get("maxPower"),
            activity_summary.get("maxPower"),
        ),
        "calories": first_present(summary.get("calories"), activity_summary.get("calories")),
    }


def stream_key(descriptor: Dict[str, Any], index: int) -> str:
    value = first_present(
        descriptor.get("key"),
        descriptor.get("metricKey"),
        descriptor.get("displayName"),
        descriptor.get("unitKey"),
        descriptor.get("metricsIndex"),
    )
    return safe_filename_part(str(value or f"metric_{index}"), max_length=40).lower()


def normalize_streams(details: Dict[str, Any]) -> Dict[str, List[Any]]:
    descriptors = details.get("metricDescriptors") if isinstance(details, dict) else None
    rows = details.get("activityDetailMetrics") if isinstance(details, dict) else None
    if not isinstance(descriptors, list) or not isinstance(rows, list):
        return {}

    keys = [
        stream_key(descriptor, index) if isinstance(descriptor, dict) else f"metric_{index}"
        for index, descriptor in enumerate(descriptors)
    ]
    streams: Dict[str, List[Any]] = {key: [] for key in keys}

    for row in rows:
        if not isinstance(row, dict):
            continue
        metrics = row.get("metrics")
        if isinstance(metrics, list):
            indexed_values = {}
            ordered_values = []
            for metric_index, metric in enumerate(metrics):
                if isinstance(metric, dict):
                    value = first_present(metric.get("value"), metric.get("directValue"))
                    indexed_values[metric.get("metricsIndex", metric_index)] = value
                    ordered_values.append(value)
                else:
                    indexed_values[metric_index] = metric
                    ordered_values.append(metric)
        else:
            indexed_values = {}
            ordered_values = [row.get(key) for key in keys]
        for index, key in enumerate(keys):
            value = indexed_values.get(index)
            if value is None and index < len(ordered_values):
                value = ordered_values[index]
            streams[key].append(value)

    return streams


def fetch_activity_payload(api: Any, summary: Dict[str, Any]) -> Dict[str, Any]:
    identifier = activity_id(summary)
    if not identifier:
        raise ValueError(f"Garmin activity has no id: {summary!r}")

    details = api.get_activity_details(identifier)
    splits = api.get_activity_splits(identifier)
    if not isinstance(details, dict):
        raise ValueError(
            f"Garmin get_activity_details({identifier}) returned "
            f"{type(details).__name__}; expected a dict"
        )

    return {
        "source": "garmin",
        "schema_version": SCHEMA_VERSION,
        "downloaded_at": utc_now().isoformat().replace("+00:00", "Z"),
        "activity": normalized_activity(summary, details),
        "streams": normalize_streams(details),
        "garmin": {
            "summary": summary,
            "details": details,
            "splits": splits,
        },
    }


def write_activity_file(
    api: Any,
    summary: Dict[str, Any],
    data_dir: Path = DATA_DIR,
) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = fetch_activity_payload(api, summary)
    path = data_dir / activity_filename(summary)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def build_garmin_client() -> Any:
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit("❌ Missing GARMIN_EMAIL or GARMIN_PASSWORD environment variables")

    try:
        from garminconnect import (
            Garmin,
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
        )
    except ImportError:
        sys.exit(
            "❌ Missing Python package garminconnect. Install it with:\n"
            "    py -3 -m pip install garminconnect"
        )

    try:
        api = Garmin(email, password)
        api.login()
        return api
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    ) as exc:
        sys.exit(f"❌ Garmin Connect login failed: {exc}")


def main() -> None:
    args = parse_args()
    state = load_state()
    cutoff = cutoff_from_state(state, args.lookback_days, args.overlap_days)

    print(f"📡 Fetching Garmin activities after {cutoff.isoformat()}")
    api = build_garmin_client()
    candidates = fetch_recent_activities(api, cutoff, args.page_limit, args.max_pages)

    seen_ids = existing_activity_ids()
    new_activities = [
        activity
        for activity in candidates
        if activity_id(activity) and activity_id(activity) not in seen_ids
    ]

    if not new_activities:
        print("No new Garmin activities.")
        if not args.dry_run:
            save_state(state, [])
        return

    print(f"Found {len(new_activities)} new Garmin activit{'y' if len(new_activities) == 1 else 'ies'}:")
    downloaded: List[Dict[str, Any]] = []
    for activity in sorted(new_activities, key=lambda item: activity_start_utc(item) or utc_now()):
        identifier = activity_id(activity)
        try:
            if args.dry_run:
                print(f"  - {identifier} | {activity_name(activity)}")
            else:
                path = write_activity_file(api, activity)
                print(f"  ✓ {identifier} | {activity_name(activity)} -> {path.name}")
            downloaded.append(activity)
        except Exception as exc:
            print(f"  ✗ Failed to process Garmin activity {identifier}: {exc}")

    if args.dry_run:
        print("\nDry run complete; no files or state were written.")
        return

    save_state(state, downloaded)
    print(f"\n✅ Downloaded {len(downloaded)} Garmin activity file(s) to {DATA_DIR}")


if __name__ == "__main__":
    main()
