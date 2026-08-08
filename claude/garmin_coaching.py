#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Garmin + Intervals.icu → Claude Coaching Brief
----------------------------------------------
Pulls the last 7 local calendar days of wellness/training-load data from
Intervals.icu plus local Garmin activity files, then formats a ready-to-paste
coaching brief for your Claude chat.

Setup:
    pip install requests pyperclip

Usage:
    python garmin_coaching.py

Config:
    Set INTERVALS_ATHLETE_ID and INTERVALS_API_KEY environment variables.
    Optionally set COACHING_TIMEZONE; defaults to America/Los_Angeles.
    Optionally set COACHING_ACTIVITY_DATA_DIR; defaults to data/activities.
"""

import json
import os
import platform
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None

    class ZoneInfoNotFoundError(Exception):
        pass

try:
    import requests
except ImportError:
    requests = None

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ATHLETE_ID_ENV = "INTERVALS_ATHLETE_ID"
API_KEY_ENV = "INTERVALS_API_KEY"
TIMEZONE_ENV = "COACHING_TIMEZONE"
ACTIVITY_DATA_DIR_ENV = "COACHING_ACTIVITY_DATA_DIR"

DEFAULT_TIMEZONE = "America/Los_Angeles"
DAYS_BACK = 7
ACTIVITY_PROBE_DAYS = 30
ACTIVITY_SOURCE_LABEL = "Local Garmin activity files"

# Optional: set to your sport + goal so the brief is pre-contextualised
SPORT = "cycling"          # e.g. cycling, running, triathlon
GOAL = "base fitness"      # e.g. race prep, weight loss, base fitness

BASE_URL = "https://intervals.icu/api/v1/athlete"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_ACTIVITY_DATA_DIR = REPO_ROOT / "data" / "activities"

FetchFn = Callable[[str, Optional[Dict[str, str]]], Any]
ActivityLoader = Callable[[str, str, str], List[Any]]
ZERO = timedelta(0)
ONE_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class ReportContext:
    report_date: date
    oldest: str
    newest: str
    timezone_name: str
    latest_wellness_date: Optional[str]
    latest_activity_date: Optional[str]
    readiness_reliable: bool
    activity_source: str
    activity_warning: str = ""
    probe_oldest: Optional[str] = None
    probe_newest: Optional[str] = None


def configured_timezone_name() -> str:
    return os.environ.get(TIMEZONE_ENV, DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE


def first_sunday_on_or_after(day: datetime) -> datetime:
    days_to_go = (6 - day.weekday()) % 7
    return day + timedelta(days=days_to_go)


def pacific_dst_range(year: int) -> Tuple[datetime, datetime]:
    # Modern US daylight-saving rules: second Sunday in March to first Sunday
    # in November. ZoneInfo is used whenever available; this is Windows fallback.
    start = first_sunday_on_or_after(datetime(year, 3, 8, 2))
    end = first_sunday_on_or_after(datetime(year, 11, 1, 2))
    return start, end


class PacificFallbackTimezone(tzinfo):
    standard_offset = timedelta(hours=-8)
    daylight_offset = timedelta(hours=-7)

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return self.standard_offset + self.dst(dt)

    def dst(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return ZERO
        naive = dt.replace(tzinfo=None)
        start, end = pacific_dst_range(naive.year)
        if start <= naive < end:
            return ONE_HOUR
        return ZERO

    def tzname(self, dt: Optional[datetime]) -> str:
        return "PDT" if self.dst(dt) else "PST"

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        naive_utc = dt.replace(tzinfo=None)
        standard_time = naive_utc + self.standard_offset
        daylight_time = naive_utc + self.daylight_offset
        start, end = pacific_dst_range(standard_time.year)
        start_utc = start - self.standard_offset
        end_utc = end - self.daylight_offset

        if start_utc <= naive_utc < end_utc:
            return daylight_time.replace(tzinfo=self)
        return standard_time.replace(tzinfo=self)


PACIFIC_FALLBACK = PacificFallbackTimezone()


def load_timezone(timezone_name: Optional[str] = None) -> tzinfo:
    tz_name = timezone_name or configured_timezone_name()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    if tz_name == DEFAULT_TIMEZONE:
        return PACIFIC_FALLBACK
    raise ValueError(
        f"Unknown time zone {tz_name!r}. Install the tzdata package for IANA "
        "timezone support on this Python installation."
    )


def local_report_date(now: Optional[Any] = None, timezone_name: Optional[str] = None) -> date:
    """Return the report date in the coaching timezone."""
    tz = load_timezone(timezone_name)
    if now is None:
        return datetime.now(tz).date()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=tz).date()
        return now.astimezone(tz).date()
    if isinstance(now, date):
        return now
    raise TypeError("now must be a date, datetime, or None")


def date_range(
    days: int,
    report_date: Optional[date] = None,
    now: Optional[Any] = None,
    timezone_name: Optional[str] = None,
) -> Tuple[str, str]:
    """Return exactly `days` inclusive local calendar dates."""
    if days < 1:
        raise ValueError("days must be at least 1")
    newest = report_date or local_report_date(now=now, timezone_name=timezone_name)
    oldest = newest - timedelta(days=days - 1)
    return oldest.isoformat(), newest.isoformat()


def preceding_date_range(first_day: date, days: int) -> Tuple[str, str]:
    newest = first_day - timedelta(days=1)
    oldest = newest - timedelta(days=days - 1)
    return oldest.isoformat(), newest.isoformat()


def intervals_credentials() -> Tuple[str, str]:
    return os.environ.get(ATHLETE_ID_ENV, ""), os.environ.get(API_KEY_ENV, "")


def validate_config() -> None:
    athlete_id, api_key = intervals_credentials()
    if (
        not athlete_id
        or not api_key
        or athlete_id == "YOUR_ATHLETE_ID"
        or api_key == "YOUR_API_KEY"
    ):
        sys.exit(
            "❌  Please set INTERVALS_ATHLETE_ID and INTERVALS_API_KEY "
            "environment variables"
        )


def fetch(path: str, params: Optional[Dict[str, str]] = None) -> Any:
    if requests is None:
        raise RuntimeError("Install the requests package before calling Intervals.icu")

    athlete_id, api_key = intervals_credentials()
    if not athlete_id or not api_key:
        raise RuntimeError(
            "Set INTERVALS_ATHLETE_ID and INTERVALS_API_KEY environment variables"
        )

    url = f"{BASE_URL}/{athlete_id}/{path}"
    resp = requests.get(url, auth=("API_KEY", api_key), params=params, timeout=10)
    if resp.status_code == 401:
        sys.exit("❌  Auth failed — check your ATHLETE_ID and API_KEY.")
    resp.raise_for_status()
    return resp.json()


def ensure_list(payload: Any, description: str) -> List[Any]:
    if not isinstance(payload, list):
        raise ValueError(
            f"{description} returned {type(payload).__name__}; expected a list"
        )
    return payload


def fetch_activity_list(fetcher: FetchFn, params: Dict[str, str]) -> List[Any]:
    return ensure_list(
        fetcher("activities", dict(params)),
        "Intervals.icu activities endpoint",
    )


def configured_activity_data_dir() -> Path:
    raw = os.environ.get(ACTIVITY_DATA_DIR_ENV)
    if not raw:
        return DEFAULT_ACTIVITY_DATA_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def is_garmin_activity_payload(payload: Dict[str, Any]) -> bool:
    activity = payload.get("activity")
    if payload.get("source") == "garmin" or isinstance(payload.get("garmin"), dict):
        return True
    return isinstance(activity, dict) and activity.get("source") == "garmin"


def read_garmin_activity_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Garmin activity file is malformed: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Garmin activity file {path} returned "
            f"{type(payload).__name__}; expected a JSON object"
        )
    if not is_garmin_activity_payload(payload):
        return None
    return payload


def load_garmin_activities(
    oldest: str,
    newest: str,
    timezone_name: str,
    data_dir: Optional[Path] = None,
) -> List[Any]:
    oldest_day = date.fromisoformat(oldest)
    newest_day = date.fromisoformat(newest)
    root = data_dir or configured_activity_data_dir()

    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"Garmin activity data path is not a directory: {root}")

    activities: List[Any] = []
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("."):
            continue
        payload = read_garmin_activity_file(path)
        if payload is None:
            continue
        activity_date = activity_local_date(payload, timezone_name)
        if activity_date is None:
            continue
        if oldest_day <= activity_date <= newest_day:
            activities.append(payload)

    return activities


def safe_avg(values: Iterable[Optional[float]], decimals: int = 1) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(mean(vals), decimals) if vals else None


def trend(values: Iterable[Optional[float]]) -> str:
    """Simple trend arrow comparing first half vs second half."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    mid = len(vals) // 2
    first_half = mean(vals[:mid]) if mid > 0 else vals[0]
    second_half = mean(vals[mid:])
    pct = ((second_half - first_half) / first_half) * 100 if first_half else 0
    if pct > 3:
        return f" ↑{abs(pct):.0f}%"
    if pct < -3:
        return f" ↓{abs(pct):.0f}%"
    return " →"


def last_value(values: Iterable[Any]) -> Any:
    return next((v for v in reversed(list(values)) if v is not None), None)


def parse_calendar_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 10:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def parse_datetime_as_local_date(value: Any, timezone_name: str) -> Optional[date]:
    tz = load_timezone(timezone_name)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return parse_calendar_date(text)
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def activity_payload(activity: Any) -> Dict[str, Any]:
    if isinstance(activity, dict) and isinstance(activity.get("activity"), dict):
        return activity["activity"]
    if isinstance(activity, dict):
        return activity
    return {}


def activity_field(activity: Any, *names: str) -> Any:
    payload = activity_payload(activity)
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    return None


def activity_local_date(activity: Any, timezone_name: str) -> Optional[date]:
    payload = activity_payload(activity)

    for field in ("start_date_local", "local_date", "date", "id"):
        parsed = parse_calendar_date(payload.get(field))
        if parsed is not None:
            return parsed

    for field in ("start_date", "startTime", "timestamp"):
        parsed = parse_datetime_as_local_date(payload.get(field), timezone_name)
        if parsed is not None:
            return parsed

    return None


def latest_activity_date(activities: Iterable[Any], timezone_name: str) -> Optional[str]:
    dates = [
        parsed
        for parsed in (activity_local_date(activity, timezone_name) for activity in activities)
        if parsed is not None
    ]
    return max(dates).isoformat() if dates else None


def wellness_local_date(wellness_record: Any) -> Optional[date]:
    if not isinstance(wellness_record, dict):
        return None
    for field in ("id", "date"):
        parsed = parse_calendar_date(wellness_record.get(field))
        if parsed is not None:
            return parsed
    return None


def latest_wellness_date(wellness: Iterable[Any]) -> Optional[str]:
    dates = [
        parsed
        for parsed in (wellness_local_date(record) for record in wellness)
        if parsed is not None
    ]
    return max(dates).isoformat() if dates else None


def format_optional_date(value: Optional[str]) -> str:
    return value or "unknown"


def build_activity_warning(
    oldest: str,
    newest: str,
    probe_oldest: str,
    probe_newest: str,
    latest_found: Optional[str],
    activity_source: str = ACTIVITY_SOURCE_LABEL,
) -> str:
    latest_line = (
        f"   Most recent activity found during probe: {latest_found}"
        if latest_found
        else "   No activities found in the current window or preceding 30 days."
    )
    return f"""
!!! ACTIVITY SYNC / DATA QUALITY WARNING !!!
   {activity_source} contain 0 activities for {oldest} → {newest}.
   Checked preceding {ACTIVITY_PROBE_DAYS} days ({probe_oldest} → {probe_newest}).
{latest_line}
   Garmin activity downloads may be missing or stale even while wellness syncs.
   ATL, CTL, and TSB readiness are unreliable; treat readiness as unknown.
""".strip()


def generate_brief(
    fetcher: FetchFn = fetch,
    activity_loader: ActivityLoader = load_garmin_activities,
    now: Optional[Any] = None,
    report_date: Optional[date] = None,
    timezone_name: Optional[str] = None,
) -> Tuple[str, ReportContext]:
    tz_name = timezone_name or configured_timezone_name()
    local_date = report_date or local_report_date(now=now, timezone_name=tz_name)
    oldest, newest = date_range(DAYS_BACK, report_date=local_date)
    params = {"oldest": oldest, "newest": newest}

    wellness = ensure_list(
        fetcher("wellness", dict(params)),
        "Intervals.icu wellness endpoint",
    )
    activities = ensure_list(
        activity_loader(oldest, newest, tz_name),
        "Garmin activity loader",
    )

    latest_wellness = latest_wellness_date(wellness)
    latest_activity = latest_activity_date(activities, tz_name)
    readiness_reliable = True
    activity_warning = ""
    probe_oldest = probe_newest = None

    if not activities:
        first_day = date.fromisoformat(oldest)
        probe_oldest, probe_newest = preceding_date_range(first_day, ACTIVITY_PROBE_DAYS)
        older_activities = ensure_list(
            activity_loader(probe_oldest, probe_newest, tz_name),
            "Garmin activity loader",
        )
        latest_activity = latest_activity_date(older_activities, tz_name)
        readiness_reliable = False
        activity_warning = build_activity_warning(
            oldest,
            newest,
            probe_oldest,
            probe_newest,
            latest_activity,
            ACTIVITY_SOURCE_LABEL,
        )

    context = ReportContext(
        report_date=local_date,
        oldest=oldest,
        newest=newest,
        timezone_name=tz_name,
        latest_wellness_date=latest_wellness,
        latest_activity_date=latest_activity,
        readiness_reliable=readiness_reliable,
        activity_source=ACTIVITY_SOURCE_LABEL,
        activity_warning=activity_warning,
        probe_oldest=probe_oldest,
        probe_newest=probe_newest,
    )
    return build_brief(wellness, activities, context), context


def build_brief(
    wellness: List[Any],
    activities: List[Any],
    context: ReportContext,
) -> str:
    # ── Wellness data (sleep, HRV, weight, resting HR) ──
    hrv_vals = [w.get("hrv") for w in wellness if isinstance(w, dict)]
    hrv_sdnn_vals = [w.get("hrvSDNN") for w in wellness if isinstance(w, dict)]
    hr_vals = [w.get("restingHR") for w in wellness if isinstance(w, dict)]
    sleep_hr_vals = [w.get("avgSleepingHR") for w in wellness if isinstance(w, dict)]
    sleep_secs = [w.get("sleepSecs") for w in wellness if isinstance(w, dict)]
    sleep_score = [w.get("sleepScore") for w in wellness if isinstance(w, dict)]
    weight_vals = [w.get("weight") for w in wellness if isinstance(w, dict)]
    sleep_hrs = [s / 3600 if s else None for s in sleep_secs]

    # ── Training load (ATL/CTL/Form) from wellness — most recent non-null entry ──
    # Intervals.icu stores daily CTL/ATL on the wellness record, not the activity.
    atl = ctl = form = None
    for w in reversed(wellness):
        if isinstance(w, dict) and w.get("ctl") is not None:
            ctl = round(w["ctl"])
            atl = round(w["atl"]) if w.get("atl") is not None else None
            form = (
                round(w["ctl"] - w["atl"])
                if (w.get("atl") is not None and w.get("ctl") is not None)
                else None
            )
            break

    # ── Activity summary ──
    act_count = len(activities)
    total_hrs = sum((activity_field(a, "moving_time") or 0) for a in activities) / 3600

    sport_counts: Dict[str, int] = {}
    for activity in activities:
        sport = activity_field(activity, "type", "sport_type") or "Other"
        sport_counts[sport] = sport_counts.get(sport, 0) + 1

    # ── Format numbers ──
    def fmt(val: Any, unit: str = "", na: str = "–") -> str:
        return f"{val}{unit}" if val is not None else na

    avg_hrv = safe_avg(hrv_vals)
    avg_sdnn = safe_avg(hrv_sdnn_vals)
    avg_hr = safe_avg(hr_vals, 0)

    # ── Last night specifically (most recent non-null value) ──
    last_hrv = last_value(hrv_vals)
    last_sdnn = last_value(hrv_sdnn_vals)
    last_sleep_hr = last_value(sleep_hr_vals)
    # HRV vs 7-day avg delta
    def delta_str(last_val: Optional[float], avg_val: Optional[float]) -> str:
        if last_val is None or avg_val is None:
            return ""
        diff = last_val - avg_val
        pct = (diff / avg_val) * 100
        sign = "+" if diff > 0 else ""
        return f"  ({sign}{pct:.0f}% vs 7d avg)"

    avg_sleep = safe_avg(sleep_hrs)
    avg_score = safe_avg(sleep_score, 0)
    latest_wt = next((w for w in reversed(weight_vals) if w), None)
    prev_wt = next((w for w in weight_vals if w), None)
    wt_delta = (
        round(latest_wt - prev_wt, 1)
        if latest_wt and prev_wt and latest_wt != prev_wt
        else None
    )

    wt_str = fmt(latest_wt, "kg")
    if wt_delta is not None:
        wt_str += f"  ({'+' if wt_delta > 0 else ''}{wt_delta}kg this week)"

    form_label = ""
    if form is not None:
        if not context.readiness_reliable:
            form_label = "Readiness unknown"
        elif form < -30:
            form_label = "⚠️  High fatigue"
        elif form < -10:
            form_label = "Training block"
        elif form < 5:
            form_label = "Neutral"
        else:
            form_label = "✅ Fresh / race-ready"

    sports_str = (
        ", ".join(f"{v}x {k}" for k, v in sport_counts.items())
        if sport_counts
        else "–"
    )
    warning_block = f"\n{context.activity_warning}\n" if context.activity_warning else ""
    unreliable_note = "  (unreliable)" if not context.readiness_reliable else ""

    # ── Build the brief ──
    brief = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Weekly Training Check-in — {context.report_date.strftime('%B %d, %Y').replace(' 0', ' ')}
   Sport: {SPORT.title()} | Goal: {GOAL.title()}
   Period: {context.oldest} → {context.newest} ({context.timezone_name})
   Latest wellness date: {format_optional_date(context.latest_wellness_date)}
   Latest activity date: {format_optional_date(context.latest_activity_date)}
   Activity source: {context.activity_source}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🫀  RECOVERY & WELLNESS
   HRV rMSSD (last night): {fmt(last_hrv, 'ms')}{delta_str(last_hrv, avg_hrv)}
   HRV SDNN  (last night): {fmt(last_sdnn, 'ms')}{delta_str(last_sdnn, avg_sdnn)}
   Avg sleeping HR:         {fmt(last_sleep_hr, ' bpm')}
   ── 7-day averages ──────────────────
   HRV rMSSD (7d avg):     {fmt(avg_hrv, 'ms')}{trend(hrv_vals)}
   HRV SDNN  (7d avg):     {fmt(avg_sdnn, 'ms')}{trend(hrv_sdnn_vals)}
   Resting HR (7d avg):    {fmt(avg_hr, ' bpm')}{trend(hr_vals)}
   Sleep (avg):        {fmt(avg_sleep, ' hrs')}{trend(sleep_hrs)}
   Sleep score (avg):  {fmt(avg_score)}
   Weight (latest):    {wt_str}
{warning_block}
⚡  TRAINING LOAD
   Activities:         {act_count} ({sports_str})
   Total time:         {total_hrs:.1f} hrs
   ATL (fatigue):      {fmt(atl)}{unreliable_note}
   CTL (fitness):      {fmt(ctl)}{unreliable_note}
   Form (TSB):         {fmt(form)}  {form_label}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Based on the above, please:
1. Assess my recovery and readiness for the coming week.
2. Flag any trends I should be aware of (HRV, sleep, weight).
3. Recommend how to structure my training this week (intensity,
   volume, rest) given my goal of {GOAL}.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""".strip()

    return brief


def output_path() -> str:
    if platform.system() == "Linux" and "ANDROID_ROOT" in os.environ:
        # Pydroid on Android — save to shared storage so any app can open it.
        return "/sdcard/coaching_brief.txt"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "coaching_brief.txt")


def save_brief(brief: str) -> None:
    path = output_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(brief)
        print(f"✅  Brief saved to: {path}")
        print("    Open it in any text app, select all, copy, paste into Claude!")
    except Exception as exc:
        print(f"⚠️  Couldn't save file ({exc}) — copy the text above manually.")


def copy_to_clipboard(brief: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(brief)
        print("✅  Also copied to clipboard!")
    except Exception:
        pass  # Clipboard unavailable on Android — file is enough.


def main() -> None:
    validate_config()

    timezone_name = configured_timezone_name()
    report_date = local_report_date(timezone_name=timezone_name)
    oldest, newest = date_range(DAYS_BACK, report_date=report_date)

    print(
        f"📡  Fetching Intervals wellness and Garmin activities "
        f"{oldest} → {newest} ({timezone_name}) ..."
    )

    try:
        brief, _context = generate_brief(
            report_date=report_date,
            timezone_name=timezone_name,
        )
    except ValueError as exc:
        sys.exit(f"❌  {exc}")

    print("\n" + brief + "\n")
    save_brief(brief)
    copy_to_clipboard(brief)


if __name__ == "__main__":
    main()
