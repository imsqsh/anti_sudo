# anti_sudo

I'm sick of checking sudo's stories.

**anti_sudo watches an Instagram account's Stories and sends the useful ones to WhatsApp**, so
you never have to open Instagram. It was built for [`zero2sudo`](https://www.instagram.com/zero2sudo/),
which posts tech internship / new-grad openings and recruiting events mixed in with everything
else. Point it at any account that posts opportunities in its Stories and set your own filters.

```text
NEW JOB                                 NEW EVENT

Cloudflare                              Capital One
Software Engineer Intern (2027)         Product Open House — Innovate & Influence

Type: 2027 Internship                   Type: Open house
Location: Austin, TX (In-Office)        When: Not listed
Compensation: Not listed                Where: Online

Apply:                                  Register:
https://job-boards.greenhouse.io/...    https://...

Source: zero2sudo                       Source: zero2sudo
```

## How it works

```text
OpenClaw automation — every 30 min, runs a command (no AI call by itself)
  └─ anti-sudo monitor
       ├─ headless Chromium (saved login) loads instagram.com/stories/<account>/
       │    and reads the Story list embedded in the page — it never opens or "views" a Story
       ├─ SQLite: skip Stories already processed, or already viewed by your account
       ├─ each NEW Story image → one vision-model call via `openclaw infer`
       │    → is it a job / event? extract company, role, location, pay, link, ...
       ├─ per-job filters from config.yaml (role type, intern/new-grad, country, pay, education)
       ├─ dedupe across Stories (same job reposted = one message)
       └─ WhatsApp via `openclaw message send`
```

## What you need

| | Notes |
|---|---|
| An Instagram account | A **secondary account** is recommended. It needs to be able to see the target's Stories. |
| A WhatsApp number for the bot | OpenClaw links to it like WhatsApp Web. A **separate number** gives real notifications; your own number works, but messages land silently in "Message yourself". |
| A vision-capable model | Anything OpenClaw supports. Built with Claude via an Anthropic API key (`anthropic/claude-opus-5-5`, roughly $0.02 per new Story). Cheaper: `anthropic/claude-sonnet-5-5`, Gemini, or local Ollama. |
| Somewhere to run it 24/7 | Docker on an always-on machine (below), or directly on macOS/Linux. |

## Make it yours: `config.yaml`

Everything personal lives in `config.yaml` (committed, no secrets) and `.env` (git-ignored).

```yaml
instagram:
  username: zero2sudo            # the account to watch
llm:
  model: anthropic/claude-opus-5-5
filtering:
  categories: [software_engineering, machine_learning, artificial_intelligence,
               data_engineering, systems, infrastructure, cybersecurity, research]
  employment_types: [internship, new_grad]
  exclude_countries: [Canada]    # drop jobs located ONLY in these countries
  min_hourly_usd: 30             # drop jobs paying less (salary / 2080); unlisted pay is kept
  education:                     # drop only if a posting EXPLICITLY excludes you
    degree_levels: [bachelors, masters]
    majors: [Computer Science, Computer Engineering, Data Science]
  send_events: true              # info sessions, hackathons, open houses, ...
```

Filtering happens **per job**: a Story listing SWE, PM and Design interns sends only the
roles that match. Preferred locations never cause a rejection. They're shown in the message
so you can decide. To track something other than tech jobs, edit the categories and the prompt
in `scripts/job_extractor.py`.

`.env` (copy from `.env.example`):

```bash
WHATSAPP_TARGET=+15555550123   # who receives alerts (E.164; must be in OpenClaw's WhatsApp allowFrom)
DRY_RUN=true                   # true = print messages instead of sending; flip when happy
TZ=America/New_York
```

## Option A — Docker (recommended for 24/7)

One container runs the OpenClaw gateway, Chromium and the monitor. State lives in two Docker
volumes, so it survives rebuilds and reboots, and no ports are exposed.

```bash
git clone https://github.com/imsqsh/anti_sudo.git && cd anti_sudo
cp .env.example .env              # set WHATSAPP_TARGET; keep DRY_RUN=true for now
# edit config.yaml
docker compose build
```

**1. Set up OpenClaw** (model + API key). Answer the prompts and skip channels and skills:

```bash
docker compose run --rm -it anti-sudo openclaw onboard --no-install-daemon
docker compose run --rm anti-sudo openclaw config set agents.defaults.heartbeat.every 0m
```

The second line turns off OpenClaw's default 30-minute "heartbeat" agent turn. It's unrelated
to this project, and each turn can use ~100K tokens.

**2. Start the gateway and link WhatsApp** (scan the QR from WhatsApp → Linked devices):

```bash
docker compose up -d
docker compose exec -it anti-sudo openclaw channels login --channel whatsapp
docker compose exec anti-sudo openclaw config set channels.whatsapp.allowFrom '["+15555550123"]' --strict-json
docker compose exec anti-sudo openclaw message send --channel whatsapp --target +15555550123 --message "test"
```

**3. Log in to Instagram.** The container has no screen, so log in on any computer with a
browser and hand the session over:

```bash
# on a computer with a display (uv sync first):
uv run python -m scripts.instagram_login --export ig-session.json
# copy it to the server if needed (scp), then:
docker compose cp ig-session.json anti-sudo:/data/ig-session.json
docker compose exec anti-sudo anti-sudo instagram_login --import /data/ig-session.json
docker compose exec anti-sudo rm /data/ig-session.json && rm ig-session.json
```

`ig-session.json` is a live login. Treat it like a password: it's git-ignored and
docker-ignored, so move it only over `scp` and delete it after importing.

**4. Test, then schedule:**

```bash
docker compose exec anti-sudo anti-sudo monitor --dry-run --limit 3   # 3 AI calls max
docker compose exec anti-sudo anti-sudo monitor --baseline            # optional: skip today's backlog
docker compose exec anti-sudo openclaw automations create --every 30m --name story-monitor \
  --command-argv '["anti-sudo","monitor"]' --timeout-seconds 900 --no-deliver
```

**5. Go live:** set `DRY_RUN=false` in `.env`, then `docker compose up -d` to apply.

### Where to host it

| Host | Cost | Instagram friendliness |
|---|---|---|
| **Always-on machine at home** (Raspberry Pi 5, old laptop, NAS, Mac mini) | hardware you have / ~$80 | **Best.** Your home connection looks like normal use. |
| Free cloud VM (e.g. Oracle Cloud Always Free, ARM) | $0 | Datacenter connections get more "confirm it's you" logouts. |
| Small cloud server (Hetzner, DigitalOcean, ...) | ~$4–6/mo | Same as above. |

The image builds for both amd64 and arm64. On a cloud VM, keep SSH key-only and the firewall
closed. The container publishes no ports.

## Option B — run directly on macOS / Linux

```bash
npm install -g openclaw                 # Node >= 24.16
openclaw onboard --install-daemon       # in a real terminal; pick your model provider
openclaw config set agents.defaults.heartbeat.every 0m
openclaw channels login --channel whatsapp
uv sync && uv run playwright install chromium
cp .env.example .env                    # BROWSER_CHANNEL=chrome uses your installed Chrome
uv run python -m scripts.instagram_login
uv run python -m scripts.monitor --dry-run --limit 3
openclaw automations create --every 30m --name story-monitor \
  --command-argv "[\"$(which uv)\",\"run\",\"--quiet\",\"python\",\"-m\",\"scripts.monitor\"]" \
  --command-cwd "$PWD" --command-env "PATH=$(dirname $(which uv)):/usr/bin:/bin" \
  --timeout-seconds 900 --no-deliver
```

This only runs while the machine is awake.

## Everyday commands

Docker: prefix with `docker compose exec anti-sudo`, e.g. `docker compose exec anti-sudo anti-sudo monitor`.
Local: `uv run python -m scripts.<name>`.

| What | Docker | Local |
|---|---|---|
| Check now | `anti-sudo monitor` | `scripts.monitor` |
| Dry run (never sends) | `anti-sudo monitor --dry-run` | `scripts.monitor --dry-run` |
| Send even if `DRY_RUN=true` | `anti-sudo monitor --live` | `scripts.monitor --live` |
| Skip the current backlog | `anti-sudo monitor --baseline` | `scripts.monitor --baseline` |
| Cap AI calls this run | `anti-sudo monitor --limit 3` | `scripts.monitor --limit 3` |
| Re-login | `anti-sudo instagram_login --import FILE` | `scripts.instagram_login` |
| Schedule history | `openclaw automations runs <id>` | same |
| Logs | `tail -f /data/logs/monitor.log` | `tail -f ~/openclaw-instagram-jobs/logs/monitor.log` |
| Tests | — | `uv run pytest` |

## Privacy & responsible use

- **Nothing personal is in the repo.** Phone number → `.env`; API key → OpenClaw's own config;
  Instagram session, database and logs → a Docker volume or `~/openclaw-instagram-jobs/`.
  All are git-ignored and docker-ignored.
- The monitor never logs cookies or passwords, never bypasses CAPTCHAs or login checks, and
  checks at most every 30 minutes. If Instagram asks you to confirm your identity, it alerts you
  and waits for you.
- Automated access may conflict with Instagram's Terms of Use. This is a personal tool for
  reading a public account at a human pace, from your own logged-in account; use it at your own
  risk, and preferably with a secondary account.
- It reads what an account posts publicly. Don't point it at private individuals.

## Development

```bash
uv sync && uv run pytest
```

MIT licensed.
