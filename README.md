# anti_sudo

I'm sick of checking sudo's stories.

Watches the `zero2sudo` Instagram Stories every 30 minutes, uses Claude (through OpenClaw)
to pick out relevant internship/new-grad jobs and recruiting events, and sends them to
WhatsApp, so Instagram never has to be opened.

## How it works

```text
OpenClaw automation (every 30m, command job — no model call by itself)
  └─ scripts/monitor.py
       ├─ headless Chrome (persistent profile) loads /stories/zero2sudo/
       │    and reads the Story list embedded in the page (never "views" a Story)
       ├─ SQLite: skip Stories already processed or already viewed on Instagram
       ├─ new Story → `openclaw infer model run` on the Story image (1 call/Story)
       ├─ per-job filters (tech role, intern/new-grad, not Canada-only, ≥ $30/hr, Master's OK)
       ├─ dedupe jobs/events in SQLite
       └─ `openclaw message send --channel whatsapp`
```

State lives in `~/openclaw-instagram-jobs/` (outside the repo): `database.sqlite`,
`browser-profile/`, `screenshots/`, `logs/monitor.log`.

## Setup

1. Install OpenClaw (`npm install -g openclaw`, Node ≥ 24.16), then in a real terminal:
   `openclaw onboard --install-daemon` and `openclaw channels login --channel whatsapp`.
   Make sure `channels.whatsapp.allowFrom` contains your number in E.164 (`+1...`).
2. `uv sync`
3. `cp .env.example .env` and set `WHATSAPP_TARGET`.
4. Log in to Instagram once: `uv run python -m scripts.instagram_login`
5. Schedule:
   ```bash
   openclaw automations create --every 30m --name zero2sudo-monitor \
     --command-argv '["/opt/homebrew/bin/uv","run","--quiet","python","-m","scripts.monitor"]' \
     --command-cwd "$PWD" --command-env "PATH=/opt/homebrew/bin:/usr/bin:/bin" \
     --timeout-seconds 900 --no-deliver
   ```

## Everyday commands

| What | Command |
|---|---|
| Check zero2sudo now | `uv run python -m scripts.monitor` |
| Dry run (never sends) | `uv run python -m scripts.monitor --dry-run` |
| Send even if `DRY_RUN=true` | `uv run python -m scripts.monitor --live` |
| Skip the current backlog | `uv run python -m scripts.monitor --baseline` |
| Cap LLM calls this run | `uv run python -m scripts.monitor --limit 3` |
| Re-login after expiry | `uv run python -m scripts.instagram_login` |
| Run the schedule now | `openclaw automations run <id>` |
| Schedule history | `openclaw automations runs <id>` |
| Logs | `tail -f ~/openclaw-instagram-jobs/logs/monitor.log` |
| Tests | `uv run pytest` |

Filters, model, and profile are in `config.yaml`. `DRY_RUN` and the WhatsApp number are in `.env`.
