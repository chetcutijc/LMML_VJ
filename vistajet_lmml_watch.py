#!/usr/bin/env python3
"""
vistajet_lmml_watch.py

Watches for VistaJet (ICAO callsign prefix "VJT") departures from Malta
International Airport (LMML), using OpenSky Network's public ADS-B data.
Writes results to docs/data.json, which the dashboard at docs/index.html
reads and displays.

Reliable:   "a VistaJet aircraft departed/is departing LMML."
Heuristic:  "this departure looks like a repositioning/empty leg."
            Public flight-tracking data has no passenger-count field, so
            this can only ever be a rough signal - see README.md for why,
            especially since LMML is VistaJet's home base.

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
CALLSIGN_PREFIX = "VJT"              # VistaJet's ICAO airline designator
MALTA_TZ = ZoneInfo("Europe/Malta")
TARGET_HOURS = (7, 12, 15)           # local times this should actually run at
TOLERANCE_MINUTES = 15               # "close enough" to a target hour
NORMAL_LOOKBACK_HOURS = 20           # covers the longest gap between checks
WIDE_LOOKBACK_HOURS = 50             # fallback if OpenSky rejects the short window
SHORT_GROUND_HOURS = 3               # below this, flag as "looks repositioned"

SEEN_FILE = Path("seen_flights.json")          # dedup only, not displayed
SEEN_RETENTION_DAYS = 14

HISTORY_FILE = Path("docs/data.json")          # what the dashboard reads
HISTORY_RETENTION_DAYS = 30

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
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"last_checked_utc": None, "last_checked_malta": None, "flights": []}


def save_history(history: dict, now_malta: datetime):
    """Always called on every real check (see main()) - even when nothing
    new was found - so the dashboard's 'last checked' time stays honest."""
    now_utc = datetime.now(timezone.utc)
    history["last_checked_utc"] = now_utc.isoformat()
    history["last_checked_malta"] = f"{now_malta:%Y-%m-%d %H:%M}"

    cutoff = now_utc - timedelta(days=HISTORY_RETENTION_DAYS)
    history["flights"] = [
        f for f in history["flights"]
        if datetime.fromisoformat(f["discovered_utc"]) >= cutoff
    ]

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


# ----------------------------------------------------------- heuristic ----
def looks_like_repositioning(departure: dict, arrivals: list) -> bool:
    """Rough signal only: short ground time before this departure. Treat
    with real skepticism - LMML is VistaJet's home base, so a short ground
    time can just as easily mean routine base operations. See README.md."""
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
    vistajet = [
        d for d in departures
        if d.get("callsign") and d["callsign"].strip().upper().startswith(CALLSIGN_PREFIX)
    ]

    seen = load_seen()
    new = [d for d in vistajet if f"{d['icao24']}-{d['firstSeen']}" not in seen]
    history = load_history()

    if new:
        arrivals = query_flights("arrival", tokens, NORMAL_LOOKBACK_HOURS)
        print(f"{now_malta:%Y-%m-%d %H:%M} - {len(new)} new VistaJet LMML departure(s):")
        for d in new:
            dep_time = datetime.fromtimestamp(d["firstSeen"], tz=timezone.utc).astimezone(MALTA_TZ)
            dest = d.get("estArrivalAirport") or "Unknown"
            flag = looks_like_repositioning(d, arrivals)
            print(f"  {d['callsign'].strip():<10} departed {dep_time:%Y-%m-%d %H:%M} -> {dest}"
                  f"{' -- looks repositioned (heuristic, not confirmed)' if flag else ''}")
            history["flights"].insert(0, {
                "callsign": d["callsign"].strip(),
                "icao24": d["icao24"],
                "departed_malta": f"{dep_time:%Y-%m-%d %H:%M}",
                "destination": dest,
                "likely_repositioning": flag,
                "discovered_utc": datetime.now(timezone.utc).isoformat(),
            })
            seen.add(f"{d['icao24']}-{d['firstSeen']}")
        save_seen(seen)
    else:
        print(f"{now_malta:%Y-%m-%d %H:%M} - no new VistaJet LMML departures.")

    save_history(history, now_malta)


if __name__ == "__main__":
    main()
