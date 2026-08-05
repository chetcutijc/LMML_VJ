# VistaJet LMML watch

Watches for VistaJet (ICAO callsign prefix `VJT`) flights departing Malta
(LMML), using OpenSky Network's free ADS-B data. Runs on a schedule and
prints newly-seen departures.

## What this can and can't actually tell you

- **Reliable:** "a VistaJet aircraft departed/is departing LMML, headed to
  [X]." This comes straight from live transponder data.
- **Not reliable:** whether a flight is genuinely empty. No public flight
  data includes a passenger count - operators don't broadcast that.
  VistaJet's own "Empty Legs" page doesn't list flights directly anymore
  either; it now points to the XO app, which is a broader marketplace
  (spans aircraft beyond just VistaJet's own fleet) with listings loaded
  client-side and no public API - not something a scheduled script can
  reliably read.
- **What this script does instead:** flags departures with unusually short
  ground time beforehand (`SHORT_GROUND_HOURS` in the script, default 3h)
  as a rough "looks repositioned" signal. Treat this with real skepticism -
  Malta is VistaJet's home base (Maltese AOC MT-17), so a short ground time
  can just as easily be routine base activity as a genuine empty leg.

If you want a *confirmed* empty leg to actually book, checking the XO app
by hand is still the most reliable route - this script is best used as an
early heads-up, not a booking tool.

## Setup

1. **Create a free OpenSky account** at opensky-network.org, then go to
   Account -> API Client and generate a `client_id` / `client_secret`.
   Anonymous access works too but has a much smaller daily credit budget,
   worth avoiding since each check calls two endpoints.

2. **Add them as GitHub Actions secrets** in your repo: Settings ->
   Secrets and variables -> Actions -> New repository secret -
   `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET`.

3. **Add the files to your repo:**
   - `vistajet_lmml_watch.py` and `requirements.txt` at the repo root
   - `vistajet-watch.yml` -> move this into `.github/workflows/`

4. **Give the workflow write access:** Settings -> Actions -> General ->
   Workflow permissions -> "Read and write permissions" (it needs this to
   commit `seen_flights.json` back after each run, so it doesn't re-alert
   you on the same flight next check).

5. **Test it manually first:** Actions tab -> "VistaJet LMML watch" -> Run
   workflow, then check the log output before trusting the schedule.

## One real risk worth knowing about

OpenSky's own docs note they may throttle or block requests from AWS and
"other hyperscalers" due to abuse from those IP ranges. GitHub-hosted
runners run on Azure, so there's a real chance requests get silently
degraded over time. If runs start failing or consistently returning empty
results, that's the likely cause. The fix is just to run the same script
somewhere else - a Raspberry Pi or any always-on device with `cron` works
identically, since the Python script itself doesn't change, only where it
runs.

## Notifications

Right now this only prints to the Actions log. Wiring up an email or
Telegram message when something new is found is a small addition on top
of this - happy to add it if you want that next.
