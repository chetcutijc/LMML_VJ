#!/usr/bin/env python3
"""
vistajet_lmml_watch.py

Watches departures from Malta International Airport (LMML) using OpenSky
Network's public ADS-B data, and sorts what it finds into two categories:

  - vistajet: VistaJet flights (ICAO callsign prefix "VJT")
  - military: flights whose ICAO24 address falls in a known-military hex
    range, per the community-maintained tar1090-db project (used by the
    popular tar1090 ADS-B web display). This can only flag aircraft that
    are broadcasting ADS-B in the first place - military aircraft that
    genuinely don't want to be tracked simply turn their transponders off,
    so this is a "what's visible" log, not a comprehensive one.

Writes results to docs/data.json, which the dashboard at docs/index.html
reads and displays as two sections.

Reliable:   "this aircraft departed/is departing LMML."
Heuristic:  for VistaJet only, "this departure looks like a
            repositioning/empty leg" - a rough signal based on short
            ground time, not a confirmed empty leg. See README.md. This
            heuristic isn't applied to the military category since the
            "empty leg charter" framing it's built on doesn't map onto
            military operations.

Meant to be triggered on a schedule (see the included GitHub Actions
workflow). It checks the current Malta local time itself and only does
real work near 07:00, 12:00 and 15:00, so the workflow can fire at extra
candidate UTC times (to survive the CET/CEST switch) without wasting API
credits on the "wrong" ones.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- config --
AIRPORT = "LMML"
VISTAJET_CALLSIGN_PREFIX = "VJT"     # VistaJet's ICAO airline designator
MILITARY_RANGES_URL = (
    "https://github.com/wiedehopf/tar1090-db/raw/refs/heads/master/ranges.json"
)
MALTA_TZ = ZoneInfo("Europe/Malta")
TARGET_HOURS = (7, 12, 15)           # local times this should actually run at
TOLERANCE_MINUTES = 15               # "close enough" to a target hour
NORMAL_LOOKBACK_HOURS = 20           # covers the longest gap between checks
WIDE_LOOKBACK_HOURS = 50             # fallback if OpenSky rejects the short window
SHORT_GROUND_HOURS = 3               # below this, flag VistaJet as "looks repositioned"

SEEN_FILE = Path("seen_flights.json")          # dedup only, not displayed
SEEN_RETENTION_DAYS = 14

HISTORY_FILE = Path("docs/data.json")          # what the dashboard reads
HISTORY_RETENTION_DAYS = 30
CATEGORIES = ("vistajet", "military")

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")


# ------------------------------------------------------------------ auth --
class TokenManager:
    """Handles OpenSky's OAuth2 client-credentials flow. Falls back to
    (much more rate-limited) anonymous access if no credentials are set."""

    def __init__(self):
        self.token = None
        self.expires_at = None

    def headers(self) -> dict:
        if not CLIENT_ID or not CLIENT_SECRET:
            return {}
        if not self.token or datetime.now(timezone.utc) >= self.expires_at:
            self._refresh()
        return {"Authorization": f"Bearer {self.token}"}

    def _refresh(self):
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        self.token = data["access_token"]
        self.expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=data.get("expires_in", 1800) - 30
        )


# ---------------------------------------------------------- opensky calls --
def query_flights(kind: str, tokens: TokenManager, hours_back: int) -> list:
    """kind is 'departure' or 'arrival'. Retries with a wider window if
    OpenSky rejects the short one - its own docs are inconsistent about
    whether this endpoint wants a MAX or a MIN span of ~2 days, so this
    handles either case rather than guessing."""
    end = int(datetime.now(timezone.utc).timestamp())
    begin = end - hours_back * 3600
    url = f"https://opensky-network.org/api/flights/{kind}"

    resp = requests.get(
        url, params={"airport": AIRPORT, "begin": begin, "end": end},
        headers=tokens.headers(), timeout=30,
    )
    if resp.status_code == 400 and hours_back != WIDE_LOOKBACK_HOURS:
        begin = end - WIDE_LOOKBACK_HOURS * 3600
        resp = requests.get(
            url, params={"airport": AIRPORT, "begin": begin, "end": end},
            headers=tokens.headers(), timeout=30,
        )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json()


# ------------------------------------------------------------ categorising --
def flight_key(d: dict) -> str:
    return f"{d['icao24']}-{d['firstSeen']}"


def is_vistajet(d: dict) -> bool:
    cs = d.get("callsign")
    return bool(cs) and cs.strip().upper().startswith(VISTAJET_CALLSIGN_PREFIX)


def load_military_ranges() -> list:
    """Best-effort fetch of known-military ICAO24 hex ranges. Returns []
    on any failure - a network hiccup here just means military detection
    is skipped for this one run, not that anything crashes."""
    try:
        r = requests.get(MILITARY_RANGES_URL, timeout=15)
        r.raise_for_status()
        ranges = r.json().get("military", [])
        return [(int(lo, 16), int(hi, 16)) for lo, hi in ranges]
    except Exception as e:
        print(f"Could not fetch military ICAO24 ranges, skipping that category this run: {e}")
        return []


def is_military(icao24: str, ranges: list) -> bool:
    if not icao24:
        return False
    val = int(icao24, 16)
    return any(lo <= val <= hi for lo, hi in ranges)


# ----------------------------------------------------- dedup state (seen) --
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set):
    cutoff = int(datetime.now(timezone.utc).timestamp()) - SEEN_RETENTION_DAYS * 86400
    pruned = {s for s in seen if int(s.rsplit("-", 1)[1]) >= cutoff}
    SEEN_FILE.write_text(json.dumps(sorted(pruned), indent=2))


# -------------------------------------------------- dashboard history data --
def load_history() -> dict:
    data = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
    data.pop("flights", None)  # drop the old single-list schema, if present
    data.setdefault("last_checked_utc", None)
    data.setdefault("last_checked_malta", None)
    for cat in CATEGORIES:
        data.setdefault(cat, [])
    return data


def save_history(history: dict, now_malta: datetime):
    """Always called on every real check (see main()) - even when nothing
    new was found - so the dashboard's 'last checked' time stays honest."""
    now_utc = datetime.now(timezone.utc)
    history["last_checked_utc"] = now_utc.isoformat()
    history["last_checked_malta"] = f"{now_malta:%Y-%m-%d %H:%M}"

    cutoff = now_utc - timedelta(days=HISTORY_RETENTION_DAYS)
    for cat in CATEGORIES:
        history[cat] = [
            f for f in history.get(cat, [])
            if datetime.fromisoformat(f["discovered_utc"]) >= cutoff
        ]

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


# ----------------------------------------------------------- heuristic ----
def looks_like_repositioning(departure: dict, arrivals: list) -> bool:
    """VistaJet only. Rough signal: short ground time before this
    departure. Treat with real skepticism - LMML is VistaJet's home base,
    so a short ground time can just as easily mean routine base
    operations. See README.md."""
    icao24 = departure.get("icao24")
    dep_time = departure.get("firstSeen")
    if not icao24 or not dep_time:
        return False
    prior_arrivals = [
        a for a in arrivals
        if a.get("icao24") == icao24 and a.get("lastSeen") and a["lastSeen"] < dep_time
    ]
    if not prior_arrivals:
        return False
    last_arrival = max(prior_arrivals, key=lambda a: a["lastSeen"])
    ground_hours = (dep_time - last_arrival["lastSeen"]) / 3600
    return 0 <= ground_hours <= SHORT_GROUND_HOURS


def record(d: dict, **extra) -> dict:
    dep_time = datetime.fromtimestamp(d["firstSeen"], tz=timezone.utc).astimezone(MALTA_TZ)
    entry = {
        "callsign": (d.get("callsign") or "").strip() or "Unknown callsign",
        "icao24": d["icao24"],
        "departed_malta": f"{dep_time:%Y-%m-%d %H:%M}",
        "destination": d.get("estArrivalAirport") or "Unknown",
        "discovered_utc": datetime.now(timezone.utc).isoformat(),
    }
    entry.update(extra)
    return entry, dep_time


# ------------------------------------------------------------------ main --
def is_check_time(now_malta: datetime) -> bool:
    return any(
        abs((now_malta - now_malta.replace(hour=h, minute=0, second=0, microsecond=0)).total_seconds())
        <= TOLERANCE_MINUTES * 60
        for h in TARGET_HOURS
    )


def main():
    now_malta = datetime.now(MALTA_TZ)
    if not is_check_time(now_malta):
        print(f"{now_malta:%Y-%m-%d %H:%M %Z} - outside the 07:00/12:00/15:00 "
              f"check window, exiting without calling the API.")
        return

    tokens = TokenManager()
    departures = query_flights("departure", tokens, NORMAL_LOOKBACK_HOURS)
    military_ranges = load_military_ranges()

    seen = load_seen()
    history = load_history()

    new_vj = [
        d for d in departures
        if is_vistajet(d) and flight_key(d) not in seen
    ]
    new_mil = [
        d for d in departures
        if is_military(d.get("icao24"), military_ranges) and flight_key(d) not in seen
    ]

    if new_vj:
        arrivals = query_flights("arrival", tokens, NORMAL_LOOKBACK_HOURS)
        print(f"{now_malta:%Y-%m-%d %H:%M} - {len(new_vj)} new VistaJet LMML departure(s):")
        for d in new_vj:
            flag = looks_like_repositioning(d, arrivals)
            entry, dep_time = record(d, likely_repositioning=flag)
            print(f"  {entry['callsign']:<10} departed {dep_time:%Y-%m-%d %H:%M} -> {entry['destination']}"
                  f"{' -- looks repositioned (heuristic, not confirmed)' if flag else ''}")
            history["vistajet"].insert(0, entry)
            seen.add(flight_key(d))

    if new_mil:
        print(f"{now_malta:%Y-%m-%d %H:%M} - {len(new_mil)} new military LMML departure(s):")
        for d in new_mil:
            entry, dep_time = record(d)
            print(f"  {entry['callsign']:<10} departed {dep_time:%Y-%m-%d %H:%M} -> {entry['destination']}")
            history["military"].insert(0, entry)
            seen.add(flight_key(d))

    if not new_vj and not new_mil:
        print(f"{now_malta:%Y-%m-%d %H:%M} - no new VistaJet or military LMML departures.")

    if new_vj or new_mil:
        save_seen(seen)

    save_history(history, now_malta)


if __name__ == "__main__":
    main()
