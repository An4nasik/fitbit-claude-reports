# fitbit-claude-reports

Daily health reports from the **Google Health API** (Fitbit / Pixel Watch) delivered to
**Telegram**, with structured AI analysis by **Claude**, trend charts, weekly digests,
long-term history and a Q&A bot. Runs entirely on **GitHub Actions** — no server required
for the core (an always-on box is optional, for reliable scheduling and the bot).

> Reports are generated in Russian out of the box — all message templates are string
> constants in `summary.py`, easy to localize.

## What you get

- **Morning report** — last night's sleep (stages, timing), resting HR, HRV, SpO2,
  respiratory rate, each with a trend vs your 7-day average.
- **Evening report** — steps, distance, calories, active zone minutes, floors,
  heart rate, workouts of the day.
- **Weekly digest** (Sunday night) — week vs previous week, workout list, long trends.
- **AI analysis** — Claude (via Claude Code CLI, your Pro/Max subscription — no API key)
  writes a structured block: dynamics, cross-metric correlations (e.g. rising resting HR +
  falling HRV + shorter sleep = early illness/overtraining signal), slow-drift warnings,
  one actionable tip.
- **Charts** — a PNG with 14/28-day panels (sleep, resting HR, HRV, steps) sent as a photo.
- **History** — every run commits metrics to `data/YYYY-MM.json`, giving analyses up to
  90 days of context (grows over time).
- **Q&A bot** — message your Telegram bot ("how was my sleep this month?"), a tiny
  long-polling script on any always-on box dispatches the workflow, Claude answers from
  your real data.

## Architecture

```
server cron / GitHub cron          GitHub Actions runner                 Telegram
  trigger.sh ──dispatch──▶  summary.py ──▶ Google Health API
  bot.py     ──dispatch──▶     │  ├─▶ claude -p (analysis, subscription auth)
                               │  ├─▶ matplotlib chart
                               │  └─▶ commit data/ back to repo
                               └───────────────▶ sendMessage / sendPhoto ──▶ you
```

## Setup

**⚠️ Keep your instance PRIVATE.** The workflow commits your health data into `data/`.
Use this repo as a template, don't fork it publicly.

### 1. Google Cloud (~10 min, free)

1. Go to https://developers.google.com/health/setup and use **Enable the API and get an
   OAuth 2.0 Client ID** (or manually: enable *Google Health API*, create an OAuth client,
   type *Web application*, redirect URI `https://www.google.com`).
2. **Audience** page: add your email to **Test users**, then press **Publish app**.
   This is mandatory: in Testing status refresh tokens die after 7 days. Verification is
   NOT needed — unverified apps work for up to 100 users.
3. **Data Access** page: add scopes `googlehealth.activity_and_fitness.readonly`,
   `googlehealth.health_metrics_and_measurements.readonly`, `googlehealth.sleep.readonly`.

### 2. Tokens

```bash
python get_refresh_token.py --client-id XXX --client-secret YYY   # Google refresh token
# Telegram: create a bot via @BotFather, send it /start, then:
python telegram_chat_id.py <BOT_TOKEN>                            # your chat_id
claude setup-token                                                # Claude subscription token
```

### 3. GitHub

Create a **private** repo from this template, push, then add Actions secrets:

| Secret | Value |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from step 1 |
| `GOOGLE_REFRESH_TOKEN` | from step 2 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | from step 2 |
| `CLAUDE_CODE_OAUTH_TOKEN` | from step 2 (optional — without it you get reports without AI analysis) |

Test: Actions → *Daily health summary* → **Run workflow**.

### 4. Scheduling

GitHub's `schedule:` cron is unreliable (runs get delayed or silently skipped).
This template triggers via `workflow_dispatch` from any always-on machine instead:

```bash
# on your box (times in UTC):
crontab -e
34 7  * * *  /opt/health-summary/trigger.sh morning
4  21 * * *  /opt/health-summary/trigger.sh evening
14 21 * * 0  /opt/health-summary/trigger.sh weekly
```

`trigger.sh` needs `.env` (see `env.example`) and `gh.token` — a fine-grained PAT with
*Actions: Read and write* for your repo. No always-on box? Add a `schedule:` block back
to the workflow and live with the delays.

### 5. Q&A bot (optional)

```bash
cp server/bot.py /opt/health-summary/
cp server/healthbot.service /etc/systemd/system/
systemctl enable --now healthbot
```

Any text message to your bot becomes a `mode=ask` workflow run; Claude answers from your
data in ~1 minute.

## CLI reference

```
python summary.py --mode morning|evening|weekly|auto [--date YYYY-MM-DD]
                  [--analyze] [--chart out.png] [--save-history data]
                  [--ask "question"] [--dry-run] [--debug] [--test-telegram]
```

Env vars: see `env.example`. `CLAUDE_MODEL=opus` picks the model (falls back to the
default model automatically if the requested one fails, e.g. usage limit hit).

## Notes & limitations

- The Google Health API launched in spring 2026 and is still evolving; the parser matches
  fields by tolerant patterns, and `--debug` dumps raw responses if something shows "n/a".
- `total-calories`, `heart-rate`, `active-minutes` rollups are limited to 14-day ranges
  (the code chunks requests automatically).
- Don't send `pageSize` to `:dailyRollUp` — the API rejects the request
  (`INVALID_ROLLUP_QUERY_DURATION`), which contradicts the docs.
- Digital Wellbeing cough/snore data (Pixel bedtime mode) is device-local; there is no
  API for it as of mid-2026.
- Not a medical device; the AI block is explicitly instructed to produce observations,
  not diagnoses.

## License

MIT
