# LMML departures watch

Watches departures from Malta (LMML) using OpenSky Network's free ADS-B
data, and sorts what it finds into two categories on a small dashboard:

- **VistaJet** - ICAO callsign prefix `VJT`
- **Military** - ICAO24 address falls in a known-military hex range

## What this can and can't actually tell you

Both categories only include aircraft that are actively broadcasting
ADS-B. Anything that isn't - including any military aircraft genuinely
trying to avoid tracking, which just turns its transponder off - simply
won't appear. This is a "what's visible" log, not a comprehensive one.

**VistaJet**: departures are reliable ("this aircraft departed LMML").
The **possible repositioning** flag is not reliable - no public flight
data includes a passenger count, and VistaJet's own "Empty Legs" page
doesn't list flights directly anymore either; it now points to the XO
app, a broader marketplace (spans aircraft beyond VistaJet's own fleet)
with no public API. So instead, departures with an unusually short gap
since that aircraft's last arrival (`SHORT_GROUND_HOURS` in the script,
default 3h) get flagged as a rough guess. Treat it with real skepticism -
Malta is VistaJet's home base, so a short ground time is often just
routine operations, not an empty leg. For a flight you could actually
book, check the XO app by hand.

**Military**: identification comes from matching each departure's ICAO24
address against [tar1090-db](https://github.com/wiedehopf/tar1090-db), a
community-maintained list of known-military address ranges (the same
data used by the popular tar1090 ADS-B web display). It's an inference
from the address block, not an official designation, and it isn't
scoped to any one country - it flags military aircraft worldwide. The
script fetches this list fresh on every run; if that fetch fails, the
military category is just skipped for that run rather than the whole
thing breaking.

## The dashboard

`docs/index.html` reads `docs/data.json` (which the script updates on
every real check) and shows both categories as separate sections, plus
when it last checked. Hosted for free via GitHub Pages - see setup step 5.

## Setup

1. **Create a free OpenSky account** at opensky-network.org, then go to
   Account -> API Client and generate a `client_id` / `client_secret`.
   Anonymous access works too but has a much smaller daily credit budget,
   worth avoiding since each check can call multiple endpoints.

2. **Add them as GitHub Actions secrets** in your repo: Settings ->
   Secrets and variables -> Actions -> New repository secret -
   `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET`.

3. **Add the files to your repo:**
   - `vistajet_lmml_watch.py` and `requirements.txt` at the repo root
   - `docs/index.html`, `docs/data.json`, and `docs/.nojekyll` in a
     `docs/` folder
   - `vistajet-watch.yml` -> move this into `.github/workflows/`

   The `.nojekyll` file is an empty file - it just needs to exist. Without
   it, GitHub Pages tries to run the `docs/` folder through Jekyll (its
   default static-site builder) and apply a default theme, which fails
   since this isn't a Jekyll site.

4. **Give the workflow write access:** Settings -> Actions -> General ->
   Workflow permissions -> "Read and write permissions" (it needs this to
   commit updated data back after each run).

5. **Turn on GitHub Pages:** Settings -> Pages -> Source: "Deploy from a
   branch" -> Branch: `main`, folder: `/docs` -> Save. GitHub gives you a
   URL like `https://<you>.github.io/<repo>/` - that's your dashboard.
   It redeploys automatically every time the workflow commits new data.

6. **Test it manually first:** Actions tab -> "VistaJet LMML watch" -> Run
   workflow, then check the log output before trusting the schedule.
   Note a manual run outside the 07:00/12:00/15:00 Malta window will
   correctly do nothing - that's by design, not a failure.

## One real risk worth knowing about

OpenSky's own docs note they may throttle or block requests from AWS and
"other hyperscalers" due to abuse from those IP ranges. GitHub-hosted
runners run on Azure, so there's a real chance requests get silently
degraded over time. If runs start failing or consistently returning empty
results, that's the likely cause. The fix is just to run the same script
somewhere else - a Raspberry Pi or any always-on device with `cron` works
identically, since the Python script itself doesn't change, only where it
runs.
