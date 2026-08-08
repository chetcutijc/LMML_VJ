#!/usr/bin/env python3
"""
vistajet_lmml_watch.py

Watches movements at Malta International Airport (LMML) using OpenSky
Network's public ADS-B data, and sorts what it finds into three
categories:

  - vistajet:   VistaJet flights (ICAO callsign prefix "VJT") - BOTH
                departures from and arrivals into LMML.
  - military:   departures whose ICAO24 address falls in a known-military
                hex range, per the community-maintained tar1090-db
                project (used by the popular tar1090 ADS-B web display).
  - commercial: everything else departing LMML - whatever isn't caught
                by the two rules above. In practice that's overwhelmingly
                scheduled airline traffic, but technically it also
                covers any other private or general-aviation departure.

Military and commercial are departures-only by design; VistaJet is the
only category tracked in both directions.

All three categories only include aircraft that are broadcasting ADS-B in
the first place. Anything that isn't - including any military aircraft
that genuinely doesn't want to be tracked, which just turns its
transponder off - simply won't appear. This is a "what's visible" log,
not a comprehensive one.

Writes results to docs/data.json, which the dashboard at docs/index.html
reads and displays as three sections. Optionally also sends a WhatsApp
message (via the official Cloud API) for each new VistaJet or military
hit - see README.md for the setup this needs. Commercial hits never
trigger a message; there are simply too many of them.

Reliable:   "this aircraft departed/arrived at LMML."
Heuristic:  for VistaJet departures only, "this looks like a
            repositioning/empty leg" - a rough signal based on short
            ground time, not a confirmed empty leg. See README.md. Not
            applied to VistaJet arrivals or to the other two categories,
            since the "empty leg charter" framing it's built on doesn't
            map onto those.

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
TARGET_HOURS = (7, 12, 13, 14, 15)           # local times this should actually run at
TOLERANCE_MINUTES = 25               # "close enough" to a target hour
NORMAL_LOOKBACK_HOURS = 20           # covers the longest gap between checks
WIDE_LOOKBACK_HOURS = 50             # fallback if OpenSky rejects the short window
SHORT_GROUND_HOURS = 3               # below this, flag a VJ departure as "looks repositioned"

SEEN_FILE = Path("seen_flights.json")          # dedup only, not displayed
SEEN_RETENTION_DAYS = 14

HISTORY_FILE = Path("docs/data.json")          # what the dashboard reads
CATEGORIES = ("vistajet", "military", "commercial")
# VistaJet and military movements are relatively rare and each one is
# individually notable, so they're worth keeping around for a month.
# Commercial traffic is high-volume and much less individually notable -
# a short window plus a hard cap keeps the file (and the page) from
# growing without bound on a busy day.
RETENTION_DAYS = {"vistajet": 30, "military": 30, "commercial": 3}
MAX_ENTRIES = {"commercial": 60}

OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")

# WhatsApp alerts are entirely optional - every function below degrades to
# a no-op (with a log line) if these aren't set, so leaving them unset
# just means no WhatsApp messages get sent. See README.md for setup.
WHATSAPP_API_VERSION = "v22.0"
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_TEMPLATE_NAME = os.environ.get("WHATSAPP_TEMPLATE_NAME", "lmml_watch_alert")
WHATSAPP_RECIPIENT_NUMBER = os.environ.get("WHATSAPP_RECIPIENT_NUMBER")


# ------------------------------------------------------------ opensky auth --
class TokenManager:
    """Handles OpenSky's OAuth2 client-credentials flow. Falls back to
    (much more rate-limited) anonymous access if no credentials are set."""

    def __init__(self):
        self.token = None
        self.expires_at = None

    def headers(self) -> dict:
        if not OPENSKY_CLIENT_ID or not OPENSKY_CLIENT_SECRET:
            return {}
        if not self.token or datetime.now(timezone.utc) >= self.expires_at:
            self._refresh()
        return {"Authorization": f"Bearer {self.token}"}

    def _refresh(self):
        r = requests.post(
            OPENSKY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": OPENSKY_CLIENT_ID,
                "client_secret": OPENSKY_CLIENT_SECRET,
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

    for cat in CATEGORIES:
        cutoff = now_utc - timedelta(days=RETENTION_DAYS[cat])
        pruned = [
            f for f in history.get(cat, [])
            if datetime.fromisoformat(f["discovered_utc"]) >= cutoff
        ]
        cap = MAX_ENTRIES.get(cat)
        if cap is not None:
            pruned = pruned[:cap]  # newest-first already, so this keeps the most recent
        history[cat] = pruned

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


# ----------------------------------------------------------- heuristic ----
def looks_like_repositioning(departure: dict, arrivals: list) -> bool:
    """VistaJet departures only. Rough signal: short ground time before
    this departure. Treat with real skepticism - LMML is VistaJet's home
    base, so a short ground time can just as easily mean routine base
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


def record(d: dict, direction: str, **extra) -> tuple:
    """direction is 'departure' or 'arrival'. A departure's relevant
    timestamp is firstSeen and relevant place is estArrivalAirport
    (destination); an arrival's relevant timestamp is lastSeen and
    relevant place is estDepartureAirport (origin)."""
    if direction == "arrival":
        event_time = datetime.fromtimestamp(d["lastSeen"], tz=timezone.utc).astimezone(MALTA_TZ)
        place = d.get("estDepartureAirport") or "Unknown"
    else:
        event_time = datetime.fromtimestamp(d["firstSeen"], tz=timezone.utc).astimezone(MALTA_TZ)
        place = d.get("estArrivalAirport") or "Unknown"

    entry = {
        "callsign": (d.get("callsign") or "").strip() or "Unknown callsign",
        "icao24": d["icao24"],
        "direction": direction,
        "time_malta": f"{event_time:%Y-%m-%d %H:%M}",
        "place": place,
        "discovered_utc": datetime.now(timezone.utc).isoformat(),
    }
    entry.update(extra)
    return entry, event_time


# ------------------------------------------------------------- whatsapp ---
def send_whatsapp_alert(headline: str, detail: str):
    """Best-effort - a WhatsApp failure should never break the rest of the
    run. Skipped silently (with one log line) if not fully configured."""
    if not (WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_RECIPIENT_NUMBER):
        print("WhatsApp not configured (missing token/phone id/recipient) - skipping alert.")
        return
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": WHATSAPP_RECIPIENT_NUMBER,
        "type": "template",
        "template": {
            "name": WHATSAPP_TEMPLATE_NAME,
            "language": {"code": "en"},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": headline[:60]},
                    {"type": "text", "text": detail[:60]},
                ],
            }],
        },
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json=payload,
            timeout=15,
        )
        if not r.ok:
            print(f"WhatsApp send failed ({r.status_code}): {r.text[:300]}")
    except Exception as e:
        print(f"WhatsApp send failed: {e}")


def describe(entry: dict) -> tuple:
    """Builds the (headline, detail) pair used for both the console log
    and the WhatsApp template's two variables."""
    what = {
        "vistajet": "VistaJet",
        "military": "Military",
        "commercial": "Commercial",
    }[entry["_category"]]
    headline = f"{what} {entry['callsign']} {entry['direction']}"
    arrow = "from" if entry["direction"] == "arrival" else "to"
    detail = f"{entry['time_malta'][-5:]} Malta {arrow} {entry['place']}"
    if entry.get("likely_repositioning"):
        detail += " (repositioning?)"
    return headline, detail


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
    arrivals = query_flights("arrival", tokens, NORMAL_LOOKBACK_HOURS)
    military_ranges = load_military_ranges()

    seen = load_seen()
    history = load_history()
    total_new = 0

    # ---- VistaJet: departures + arrivals, merged and time-sorted ----
    new_vj_dep = [d for d in departures if is_vistajet(d) and flight_key(d) not in seen]
    new_vj_arr = [a for a in arrivals if is_vistajet(a) and flight_key(a) not in seen]

    vj_batch = []
    for d in new_vj_dep:
        flag = looks_like_repositioning(d, arrivals)
        entry, event_time = record(d, "departure", likely_repositioning=flag)
        vj_batch.append((event_time, entry))
    for a in new_vj_arr:
        entry, event_time = record(a, "arrival")
        vj_batch.append((event_time, entry))

    if vj_batch:
        vj_batch.sort(key=lambda pair: pair[0], reverse=True)
        print(f"{now_malta:%Y-%m-%d %H:%M} - {len(vj_batch)} new VistaJet LMML movement(s):")
        for _, entry in vj_batch:
            entry["_category"] = "vistajet"
            headline, detail = describe(entry)
            print(f"  {headline} | {detail}")
            send_whatsapp_alert(headline, detail)
            del entry["_category"]
        history["vistajet"] = [e for _, e in vj_batch] + history["vistajet"]
        for d in new_vj_dep:
            seen.add(flight_key(d))
        for a in new_vj_arr:
            seen.add(flight_key(a))
        total_new += len(vj_batch)

    # ---- Military / commercial: departures only, unchanged scope ----
    new_mil, new_other = [], []
    for d in departures:
        if is_vistajet(d) or flight_key(d) in seen:
            continue
        (new_mil if is_military(d.get("icao24"), military_ranges) else new_other).append(d)

    for cat, items, alert in (("military", new_mil, True), ("commercial", new_other, False)):
        if not items:
            continue
        batch = [record(d, "departure") for d in items]
        batch.sort(key=lambda pair: pair[1], reverse=True)
        print(f"{now_malta:%Y-%m-%d %H:%M} - {len(batch)} new {cat} LMML departure(s):")
        for entry, _ in batch:
            entry["_category"] = cat
            headline, detail = describe(entry)
            print(f"  {headline} | {detail}")
            if alert:
                send_whatsapp_alert(headline, detail)
            del entry["_category"]
        history[cat] = [e for e, _ in batch] + history[cat]
        for d in items:
            seen.add(flight_key(d))
        total_new += len(batch)

    if total_new == 0:
        print(f"{now_malta:%Y-%m-%d %H:%M} - no new movements in any category.")
    else:
        save_seen(seen)

    save_history(history, now_malta)


if __name__ == "__main__":
    main()
