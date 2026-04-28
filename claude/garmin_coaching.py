#!/usr/bin/env python3
"""
Garmin → Intervals.icu → Claude Coaching Brief
-----------------------------------------------
Pulls the last 7 days of wellness + training data from Intervals.icu
and formats a ready-to-paste coaching brief for your Claude chat.

Setup:
    pip install requests pyperclip

Usage:
    python garmin_coaching_brief.py

Config:
    Set your ATHLETE_ID and API_KEY below (or use environment variables).
"""

import os
import sys
import requests
from datetime import date, timedelta
from statistics import mean

# ─────────────────────────────────────────────
# CONFIG — fill these in or set as env vars
# ─────────────────────────────────────────────
ATHLETE_ID = '5019'
API_KEY    = '6es9m7qbyr07ptqcc35yjf9xz'
DAYS_BACK  = 7   # how many days to look back

# Optional: set to your sport + goal so the brief is pre-contextualised
SPORT = "cycling"          # e.g. cycling, running, triathlon
GOAL  = "base fitness"     # e.g. race prep, weight loss, base fitness
# ─────────────────────────────────────────────


BASE_URL = "https://intervals.icu/api/v1/athlete"
AUTH     = ("API_KEY", API_KEY)


def date_range(days: int):
    today = date.today()
    oldest = today - timedelta(days=days)
    return oldest.isoformat(), today.isoformat()


def fetch(path: str, params: dict = None):
    url = f"{BASE_URL}/{ATHLETE_ID}/{path}"
    resp = requests.get(url, auth=AUTH, params=params, timeout=10)
    if resp.status_code == 401:
        sys.exit("❌  Auth failed — check your ATHLETE_ID and API_KEY.")
    resp.raise_for_status()
    return resp.json()


def safe_avg(values, decimals=1):
    vals = [v for v in values if v is not None]
    return round(mean(vals), decimals) if vals else None


def trend(values):
    """Simple trend arrow comparing first half vs second half."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    mid = len(vals) // 2
    first_half  = mean(vals[:mid]) if mid > 0 else vals[0]
    second_half = mean(vals[mid:])
    pct = ((second_half - first_half) / first_half) * 100 if first_half else 0
    if pct > 3:
        return f" ↑{abs(pct):.0f}%"
    elif pct < -3:
        return f" ↓{abs(pct):.0f}%"
    return " →"


def main():
    if ATHLETE_ID == "YOUR_ATHLETE_ID" or API_KEY == "YOUR_API_KEY":
        sys.exit(
            "❌  Please set your ATHLETE_ID and API_KEY in the script "
            "or as environment variables:\n"
            "    export INTERVALS_ATHLETE_ID=i00000\n"
            "    export INTERVALS_API_KEY=your_key_here"
        )

    oldest, newest = date_range(DAYS_BACK)
    params = {"oldest": oldest, "newest": newest}

    print(f"📡  Fetching data {oldest} → {newest} ...")

    # ── Wellness data (sleep, HRV, weight, resting HR) ──
    wellness = fetch("wellness", params)

    hrv_vals      = [w.get("hrv")           for w in wellness]  # overnight rMSSD (Garmin)
    hrv_sdnn_vals = [w.get("hrvSDNN")         for w in wellness]  # overnight SDNN
    hr_vals       = [w.get("restingHR")       for w in wellness]
    sleep_hr_vals = [w.get("avgSleepingHR")   for w in wellness]  # avg HR during sleep
    sleep_secs    = [w.get("sleepSecs")       for w in wellness]
    sleep_score   = [w.get("sleepScore")      for w in wellness]
    weight_vals   = [w.get("weight")          for w in wellness]
    dates         = [w.get("id")              for w in wellness]

    sleep_hrs   = [s / 3600 if s else None for s in sleep_secs]

    # Uncomment to debug what Intervals.icu is actually returning:
    # import json; print(json.dumps(wellness[-1], indent=2))

    # ── Training load (ATL/CTL/Form) from wellness — most recent non-null entry ──
    # Intervals.icu stores daily CTL/ATL on the wellness record, not the activity
    atl = ctl = form = None
    for w in reversed(wellness):
        if w.get("ctl") is not None:
            ctl  = round(w["ctl"])
            atl  = round(w["atl"]) if w.get("atl") is not None else None
            form = round(w["atl"] - w["ctl"]) if (w.get("atl") is not None and w.get("ctl") is not None) else None
            break

    # ── Activity summary ──
    activities = fetch("activities", params)

    # ── Activity summary ──
    act_count    = len(activities)
    total_hrs    = sum((a.get("moving_time") or 0) for a in activities) / 3600
    total_kj     = sum((a.get("total_elevation_gain") or 0) for a in activities)  # placeholder

    # Separate sport types
    sport_counts = {}
    for a in activities:
        s = a.get("type", "Other")
        sport_counts[s] = sport_counts.get(s, 0) + 1

    # ── Format numbers ──
    def fmt(val, unit="", na="–"):
        return f"{val}{unit}" if val is not None else na

    avg_hrv      = safe_avg(hrv_vals)
    avg_sdnn     = safe_avg(hrv_sdnn_vals)
    avg_sleep_hr = safe_avg(sleep_hr_vals, 0)
    avg_hr       = safe_avg(hr_vals, 0)
    avg_sleep  = safe_avg(sleep_hrs)
    avg_score  = safe_avg(sleep_score, 0)
    latest_wt  = next((w for w in reversed(weight_vals) if w), None)
    prev_wt    = next((w for w in weight_vals if w), None)
    wt_delta   = round(latest_wt - prev_wt, 1) if latest_wt and prev_wt and latest_wt != prev_wt else None

    wt_str = fmt(latest_wt, "kg")
    if wt_delta is not None:
        wt_str += f"  ({'+' if wt_delta > 0 else ''}{wt_delta}kg this week)"

    form_label = ""
    if form is not None:
        if form < -30:   form_label = "⚠️  High fatigue"
        elif form < -10: form_label = "Training block"
        elif form < 5:   form_label = "Neutral"
        else:            form_label = "✅ Fresh / race-ready"

    sports_str = ", ".join(f"{v}x {k}" for k, v in sport_counts.items()) if sport_counts else "–"

    # ── Build the brief ──
    brief = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Weekly Training Check-in — {date.today().strftime('%B %d, %Y').replace(' 0', ' ')}
   Sport: {SPORT.title()} | Goal: {GOAL.title()}
   Period: {oldest} → {newest}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🫀  RECOVERY & WELLNESS
   HRV rMSSD (avg):    {fmt(avg_hrv, 'ms')}{trend(hrv_vals)}
   HRV SDNN (avg):     {fmt(avg_sdnn, 'ms')}{trend(hrv_sdnn_vals)}
   Avg sleeping HR:    {fmt(avg_sleep_hr, ' bpm')}{trend(sleep_hr_vals)}
   Resting HR (avg):   {fmt(avg_hr, ' bpm')}{trend(hr_vals)}
   Sleep (avg):        {fmt(avg_sleep, ' hrs')}{trend(sleep_hrs)}
   Sleep score (avg):  {fmt(avg_score)}
   Weight (latest):    {wt_str}

⚡  TRAINING LOAD
   Activities:         {act_count} ({sports_str})
   Total time:         {total_hrs:.1f} hrs
   ATL (fatigue):      {fmt(atl)}
   CTL (fitness):      {fmt(ctl)}
   Form (TSB):         {fmt(form)}  {form_label}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Based on the above, please:
1. Assess my recovery and readiness for the coming week.
2. Flag any trends I should be aware of (HRV, sleep, weight).
3. Recommend how to structure my training this week (intensity,
   volume, rest) given my goal of {GOAL}.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""".strip()

    # ── Output ──
    print("\n" + brief + "\n")

    # ── Save to file (works on all platforms including Pydroid on Android) ──
    import platform
    if platform.system() == "Linux" and "ANDROID_ROOT" in os.environ:
        # Pydroid on Android — save to shared storage so any app can open it
        output_path = "/sdcard/coaching_brief.txt"
    else:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coaching_brief.txt")

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(brief)
        print(f"✅  Brief saved to: {output_path}")
        print("    Open it in any text app, select all, copy, paste into Claude!")
    except Exception as e:
        print(f"⚠️  Couldn't save file ({e}) — copy the text above manually.")

    # ── Also try clipboard (works on desktop, silently skipped on Android) ──
    try:
        import pyperclip
        pyperclip.copy(brief)
        print("✅  Also copied to clipboard!")
    except Exception:
        pass  # Clipboard unavailable on Android — file is enough


if __name__ == "__main__":
    main()
