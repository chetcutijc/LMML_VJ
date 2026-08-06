# LMML departures watch

Watches departures from Malta (LMML) using OpenSky Network's free ADS-B
data, and sorts what it finds into three categories on a small dashboard:

- **VistaJet** - ICAO callsign prefix `VJT`, both arrivals and departures
- **Military** - ICAO24 address falls in a known-military hex range (departures only)
- **Commercial** - everything else departing (mostly scheduled airline
  traffic, but technically any departure not caught by the two rules above)

## What this can and can't actually tell you

All three categories only include aircraft that are actively broadcasting
ADS-B. Anything that isn't - including any military aircraft genuinely
trying to avoid tracking, which just turns its transponder off - simply
won't appear. This is a "what's visible" log, not a comprehensive one.

**VistaJet**: arrivals and departures are both reliable ("this aircraft
arrived at / departed LMML"). The **possible repositioning** flag is not
reliable, and only ever applies to departures - no public flight data
includes a passenger count, and VistaJet's own "Empty Legs" page doesn't
list flights directly anymore either; it now points to the XO app, a
broader marketplace (spans aircraft beyond VistaJet's own fleet) with no
public API. So instead, departures with an unusually short gap since
that aircraft's last arrival (`SHORT_GROUND_HOURS` in the script,
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

**Commercial**: whatever isn't VistaJet or military. There's a lot more
of it than the other two categories, so it's kept for 3 days (not 30)
and capped at the 60 most recent entries - both configurable at the top
of the script (`RETENTION_DAYS`, `MAX_ENTRIES`).

## The dashboard

`docs/index.html` reads `docs/data.json` (which the script updates on
every real check) and shows all three categories as separate sections,
plus when it last checked. Hosted for free via GitHub Pages - see setup
step 5.

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

## WhatsApp alerts (optional)

New VistaJet or military hits can also send a WhatsApp message, using
Meta's official Cloud API. This is meaningfully more setup than anything
else here - budget maybe 20-30 minutes of active setup, plus template
approval time on top (often just minutes, but not guaranteed). Commercial
hits never trigger a message; there's too much of it.

1. **Create a free Meta Developer account** at developers.facebook.com,
   then create a new App and add the **WhatsApp** product to it. This
   automatically provisions a WhatsApp Business Account and a free test
   phone number - you don't need your own dedicated number for personal
   use.

2. **Add your own number as a test recipient.** On the app's WhatsApp ->
   API Setup page, add your WhatsApp number under "To" (up to 5 allowed
   on the test number). Meta sends a verification code to confirm it.

3. **Send yourself the sample "hello_world" template** from that same
   page to confirm the connection works end to end.

4. **Create a permanent access token.** The temporary one from step 3
   expires in 24 hours, which would silently break the schedule the very
   next day. Go to Business Settings -> System Users -> Add, create a
   system user, click "Assign Assets", give it Full Control over both
   your app and your WhatsApp Business Account, then click "Generate
   token" on that system user with no expiration set. Save it somewhere
   safe - it won't be shown again.

5. **Create a message template.** Proactive messages (the bot messaging
   you first, rather than replying to you) need Meta's pre-approval -
   free-form text only works within 24 hours of you messaging the bot,
   which isn't a realistic way to run an automated watcher. In WhatsApp
   Manager -> Message Templates, create a new template:
   - Category: **Utility**
   - Name: `lmml_watch_alert` (or anything - just match it in step 7)
   - Body: `LMML Watch: {{1}} - {{2}}`

   Submit for review.

6. **Note down three values:** the **Phone number ID** (API Setup page),
   your **permanent access token** (step 4), and your own WhatsApp
   number in international format with no `+` or spaces
   (e.g. `35699112233`).

7. **Add GitHub Actions secrets** (Settings -> Secrets and variables ->
   Actions): `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and
   `WHATSAPP_RECIPIENT_NUMBER` are required. `WHATSAPP_TEMPLATE_NAME` is
   optional - it already defaults to `lmml_watch_alert` in the script, so
   only add it if you named your template something else.

If any of the required secrets aren't set, the script just skips sending
WhatsApp messages and logs a line saying so - it won't break the rest of
the run.

**Cost**: messages sent within 24 hours of you messaging the bot are
free. Outside that window - the normal case for an automated alert - Meta
charges a small per-template fee, roughly $0.001-0.08 depending on your
country, billed through your Meta Business account. For occasional
alerts like this it's effectively pocket change, but it isn't literally
free.

## One real risk worth knowing about

OpenSky's own docs note they may throttle or block requests from AWS and
"other hyperscalers" due to abuse from those IP ranges. GitHub-hosted
runners run on Azure, so there's a real chance requests get silently
degraded over time. If runs start failing or consistently returning empty
results, that's the likely cause. The fix is just to run the same script
somewhere else - a Raspberry Pi or any always-on device with `cron` works
identically, since the Python script itself doesn't change, only where it
runs.
