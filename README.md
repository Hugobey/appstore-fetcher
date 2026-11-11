# App Store prospecting CLI

Local Python tool that finds B2C mobile apps, matches them to an ICP, and appends new rows to an existing Google Sheets CRM.

Scraping does not send email. Outreach is a separate command, and only after you confirm it.

## Install

Python 3.11 or newer is required. The project venv uses Homebrew Python 3.12.

```bash
cd /Users/hugoblanchart/Desktop/PROJECTS/prospecting-tool
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run the tool with `./fetch`. That script uses the project virtualenv, so the system Python 3.9 is not used.

## Credentials

Nothing in the source code contains an API key. These files are gitignored.

### Google Sheets

1. In Google Cloud, create or choose a project and enable the Google Sheets API.
2. Create a service account and download its JSON key.
3. Save the key as `credentials/google-service-account.json`. An example shape is in `credentials/google-service-account.example.json`.
4. Open the key and copy `client_email`.
5. Share this spreadsheet with that address as an Editor:

   https://docs.google.com/spreadsheets/d/1ZWAZ5H1d6mo649vaGXlblVbfKms0EiLsphdcTtWEmiQ/edit

The spreadsheet id is `spreadsheet_id` in `config/settings.json`. Leave `worksheet_name` empty to use the first worksheet.

The sheet must already have `App Name` and `App Store URL` in the header row (within the first 10 rows). Existing rows are never overwritten or deleted. Extra columns are added only to the right when they are missing: Lead Score, Email Source, Email Type, Platform, Ratings, Review Count, Category, App Store ID, Developer Website, Notes.

If the sheet cannot be opened, the program stops. It will not scrape without a deduplication list.

### Apify

Create a token at https://console.apify.com/account/integrations and either:

```bash
export APIFY_TOKEN="your-token"
```

or save that single line in `credentials/apify_token`.

iOS search uses the actor `epctex/appstore-scraper`. Android search uses `epctex/google-play-scraper`. The Google Play actor's API is not available on the Apify free plan (demo mode is limited and blocks the API). On a free plan the wizard warns and can switch the run to iOS only.

Each search is capped at `max_charge_usd_per_search` (default $0.25) through Apify's `maxTotalChargeUsd`. A run starts at most `max_searches_per_run` searches (default 6) of `max_items_per_search` apps (default 40). The printed estimate uses the published per-search and per-item fees. Successful searches are cached for 24 hours under `data/search_cache/` so the same keyword is not billed again the same day.

Reviews are not requested. Rating count and written-review count stay separate. On iOS, `userRatingCount` is the rating count. A public iTunes lookup can fill gaps such as the description after discovery. It does not replace Apify search, and it does not overwrite values the actor already returned. Turn it off with `itunes_lookup_fallback` in `config/settings.json`.

### Gmail, only if you send mail

Sending is optional. Create an OAuth desktop client, enable the Gmail API, and save the client JSON as `credentials/gmail-oauth.json` (see `credentials/gmail-oauth.example.json`). The first send opens a browser and stores `credentials/gmail-token.json`. Scopes are `gmail.send` and `gmail.settings.basic`. The primary Gmail signature is appended when the draft does not already contain it.

## Daily use

```bash
cd /Users/hugoblanchart/Desktop/PROJECTS/prospecting-tool
./fetch
```

On the first run the wizard asks eight questions, then shows a summary. Choose Start scraping, Edit configuration, or Cancel. Start saves the answers to `config/targeting.json`. Later runs load those answers and offer Continue to the summary, Review each question, or Cancel. You do not have to answer every question again. Cancel discards unsaved edits.

Before the wizard, a real run connects to the CRM, counts existing leads, and builds the exclusion set. The primary key is the App Store id (`ios:123` or `android:com.example.app`). The canonical store URL is the secondary key.

The wizard then checks the Apify account. A zero balance stops the run. A balance under `min_remaining_usd` (default $0.05) asks before continuing. If the limits call fails, the error is shown and scraping continues only if you confirm.

Discovery stops starting new searches once the target number of qualified prospects is reached. The current search is finished, so the list can be a little larger than the target. Apps already in the CRM are excluded. Hard filters (ratings, age, last update, ICP) run before scoring. A missing field does not by itself fail a filter.

After enrichment and scoring you get a results summary and a preview. You can approve all, choose rows, exclude rows, inspect a lead, or also import score-passing apps that have no public email. Import appends rows. New rows with an email get status `New`. Rows without an email get `Missing email`. An address on the suppression list is stored as `Suppressed` with an empty subject and body, so it is remembered and never queued to send.

## Change the strategy without editing Python

| What | Where |
|---|---|
| ICP, platforms, categories, ratings, age, updates, score, volume, country, template | The wizard. Saved in `config/targeting.json`. |
| Points, bands, and turning a criterion off | `config/scoring.json`. Set `"enabled": false` to ignore a criterion. |
| Search keywords for each category | `category_keywords` in `config/settings.json`. |
| Email wording | `templates/emails.json`. Variables are `{{App Name}}`, `{{Contact Name}}`, and `{{Category}}`. A blank contact name becomes `Hi,`. |
| Daily send cap | `outreach.daily_limit` in `config/settings.json` (default 20). Counted from `Sent Date` on the sheet. |

The score is `points / known maximum`, scaled to 100. Unknown facts are listed and left out of that fraction. They are not scored as zero. `minimum_known_max` (default 40) stops a lead from passing when too little is known to score. Set it to `0` to disable that gate. Only leads at or above the wizard's minimum score are offered as qualified.

The first question asks which ICP you are targeting: Solo developer, Agency, Startups, or Scaleup. A lead is kept when its classification matches that choice. A lead that cannot be classified is kept, because a missing signal does not fail the filter. A blocklisted publisher (Google, Spotify, and the rest of the list in settings) is always excluded.

Classification uses the developer name, the description, and traction:

- Agency language wins first. The name or description has to say agency, consultancy, or client work such as "we build apps" or "for our clients". A studio or labs name alone is a product company, not an agency.
- Revenue is used when an actor sends it. The current App Store and Google Play actors do not. `$500,000` or more is a scaleup. `$20,000` up to that line is startup traction.
- Google Play sends `minInstalls`, or a string such as `100,000+`. `100,000` installs or more is a scaleup. `1,000` to `99,999` is startup traction. Under `1,000` is early.
- The App Store actor does not send downloads. Rating count stands in: `5,000` or more is a scaleup, `100` to `4,999` is startup traction, and under `100` is early.
- Scaleup traction wins over a personal name. A person's name, or a legal suffix on a person's name, stays a solo developer only while traction is still early. A brand, a legal suffix on a brand, or a studio name is a startup unless traction says scaleup.

The cutoffs live in `developer_classification` in `config/settings.json`. Rating and install score bands are per ICP in `config/scoring.json`. Installs and ratings are left out of an agency's score.

The ratings question is still a separate cap. For Startups, Scaleup, and Agency, a saved cap of 10–100 is not the highlighted default, and No maximum is available. A cap of 30 would remove those apps before the ICP check.

Broad category words tend to return popular apps first. The default keyword lists are narrower (for example `focus timer` and `habit tracker`). Edit those lists, or add custom keywords in the wizard, when a search is too crowded. `end_page` in settings controls how far each search goes.

## Website enrichment

For a developer website the tool fetches the homepage, `/contact`, `/privacy`, `/privacy-policy`, `/terms`, and a few contact links found on those pages. It reads `robots.txt` and skips disallowed URLs. A failed robots fetch is logged and only a small page set is tried. HTTP 401, 403, and 429 stop that site. Redirects are checked manually, and private, local, and non-global addresses are refused.

Emails come only from `mailto:` links, visible text, obfuscated `name [at] domain [dot] com` text, and Cloudflare `data-cfemail` values that decode to a real address. Addresses are not guessed. `noreply` addresses are dropped. A founder label requires nearby founder or CEO language plus a non-generic mailbox, or a solo developer's full name matching the mailbox (first name alone is not enough). Support and general mailboxes are labeled from the local part. No email leaves the prospect available with status `Missing email`.

## Outreach

Scraping never sends mail. After you edit `Email Subject` and `Generated Body` in the sheet:

```bash
./fetch send
```

The command prints each draft, defaults to cancel, and asks a second time before calling Gmail. There is no delay between messages. The daily limit is the one in settings. Messages are not sent to suppressed addresses.

```bash
./fetch suppress person@example.com --reason "asked to stop"
./fetch unsuppress person@example.com
```

The list is `data/suppression.json`. Someone who asks to stop should be added there and not contacted again.

## Other commands

```bash
./fetch check          # CRM connection and lead count only
./fetch --demo         # wizard plus sample apps; no Apify and no sheet writes
.venv/bin/python -m unittest discover -s tests
```

`check` and a normal run both fail closed when the service account file is missing.

## Recovery

Ctrl+C during an Apify run aborts that run and keeps items already returned. Raw actor items are appended to `data/sessions/` as they arrive, and `data/session.json` records the stage. The next launch offers to resume a `running` or `ready` session. A ready session opens the preview again without repeating the searches. An imported session is not offered again.

Logs go to `logs/prospecting.log` only, so the wizard stays readable. Known failures print the reason and exit 1. Unexpected failures log a traceback and print a short message.

## Layout

```text
fetch                   short command; runs main.py with the project virtualenv
main.py                 commands and the daily run
config/targeting.json   last confirmed wizard answers
config/settings.json    sheet, actors, keywords, limits
config/scoring.json     points and criterion switches
modules/                wizard, CRM, scrape, enrich, score, import
templates/emails.json   subject and body templates
credentials/            service account, Apify token, Gmail OAuth
tests/                  unit tests, including a demo discovery
```
