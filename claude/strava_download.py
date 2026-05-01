#!/usr/bin/env python3
"""
Strava Activity Downloader
---------------------------
Fetches new activities from Strava (originally uploaded from Garmin)
and saves them as FIT/TCX files to data/activities/ for tracking.

Tracks last-run timestamp in data/activities/.last_fetch.json so each
run only downloads new activities.

Required env vars:
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    STRAVA_REFRESH_TOKEN
"""

import os
import sys
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

CLIENT_ID = os.environ.get('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.environ.get('STRAVA_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('STRAVA_REFRESH_TOKEN')

if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
    sys.exit("❌ Missing Strava credentials in environment")

# Repo root assumed to be 2 levels up from this script (claude/strava_download.py)
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "activities"
STATE_FILE = DATA_DIR / ".last_fetch.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_access_token():
    """Exchange refresh token for fresh access token."""
    resp = requests.post('https://www.strava.com/oauth/token', data={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()['access_token']


def load_state():
    """Load timestamp of last successful fetch."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    # First run: fetch last 30 days
    return {'last_fetch_epoch': int(time.time()) - 30 * 24 * 3600}


def save_state(epoch):
    """Persist timestamp of last successful fetch."""
    STATE_FILE.write_text(json.dumps({'last_fetch_epoch': epoch}, indent=2))


def list_activities_since(token, after_epoch):
    """List all activities after a given epoch timestamp."""
    activities = []
    page = 1
    while True:
        resp = requests.get(
            'https://www.strava.com/api/v3/athlete/activities',
            headers={'Authorization': f'Bearer {token}'},
            params={'after': after_epoch, 'per_page': 100, 'page': page},
            timeout=15
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return activities


def download_original(token, activity_id, dest_dir):
    """Download the original uploaded file (FIT or TCX) for an activity."""
    # Strava's "export_original" endpoint requires the web cookie session,
    # NOT the API token. The API alternative is to reconstruct from streams.
    # 
    # However, the API DOES provide a download via the activity streams endpoint.
    # We'll fetch streams + activity metadata and save as TCX.
    
    # Get activity details
    act_resp = requests.get(
        f'https://www.strava.com/api/v3/activities/{activity_id}',
        headers={'Authorization': f'Bearer {token}'},
        timeout=15
    )
    act_resp.raise_for_status()
    activity = act_resp.json()
    
    # Get streams (time, distance, latlng, altitude, heartrate, watts, cadence)
    stream_resp = requests.get(
        f'https://www.strava.com/api/v3/activities/{activity_id}/streams',
        headers={'Authorization': f'Bearer {token}'},
        params={'keys': 'time,distance,latlng,altitude,heartrate,watts,cadence', 'key_by_type': 'true'},
        timeout=30
    )
    stream_resp.raise_for_status()
    streams = stream_resp.json()
    
    # Save as JSON for easy parsing later
    start_dt = datetime.fromisoformat(activity['start_date'].replace('Z', '+00:00'))
    safe_name = activity['name'].replace('/', '_').replace(' ', '_')[:40]
    filename = f"{start_dt.strftime('%Y-%m-%d_%H%M')}_{activity_id}_{safe_name}.json"
    filepath = dest_dir / filename
    
    output = {
        'activity': activity,
        'streams': streams
    }
    filepath.write_text(json.dumps(output, indent=2))
    return filepath


def main():
    state = load_state()
    after_epoch = state['last_fetch_epoch']
    print(f"📡 Fetching activities since {datetime.fromtimestamp(after_epoch).isoformat()}")
    
    token = get_access_token()
    activities = list_activities_since(token, after_epoch)
    
    if not activities:
        print("No new activities.")
        # Still update state so we don't refetch the same window forever
        save_state(int(time.time()))
        return
    
    print(f"Found {len(activities)} new activit{'y' if len(activities)==1 else 'ies'}:")
    
    downloaded = []
    for act in sorted(activities, key=lambda a: a['start_date']):
        try:
            path = download_original(token, act['id'], DATA_DIR)
            print(f"  ✓ {act['start_date'][:16]} | {act['type']:20s} | {act['name']}")
            downloaded.append(path.name)
        except Exception as e:
            print(f"  ✗ Failed to download {act['id']}: {e}")
    
    # Save state at most-recent activity time, plus 1 second
    if downloaded:
        latest = max(activities, key=lambda a: a['start_date'])
        latest_epoch = int(datetime.fromisoformat(
            latest['start_date'].replace('Z', '+00:00')
        ).timestamp()) + 1
        save_state(latest_epoch)
        print(f"\n✅ Downloaded {len(downloaded)} file(s) to {DATA_DIR}")
    else:
        print("\n⚠️ No files successfully downloaded")


if __name__ == "__main__":
    main()