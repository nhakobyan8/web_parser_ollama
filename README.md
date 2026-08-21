# Ollama Web Posts Monitor Bot

A fully local, multi-user publication monitoring system. The Telegram bot manages users, sources, polling intervals, three AI prompts, and output methods. All AI analysis runs locally through Ollama.

## Features

- Administrators can add, enable, and disable users by Telegram ID.
- Every user has separate sources, intervals, prompts, channel settings, and CSV output.
- Universal source URLs replace site-specific parsers.
- Three-stage discovery → extraction → processing pipeline through Ollama.
- Structured article extraction enforced by JSON Schema.
- Atomic JSON database at `/app/data/users.json`.
- Persistent source state, errors, and processed URL history.
- Separate CSV report for every user.
- Publication through the control bot or a separate publisher bot.
- Public URL validation, SSRF protection, redirect control, and response-size limits.
- Standard HTTP client with Cloudscraper browser-mode fallback.
- Docker Compose configuration for CPU and an NVIDIA GPU override.

## Requirements

- Docker Engine with the Docker Compose plugin, or Docker Desktop.
- At least one Telegram bot token.
- A numeric Telegram ID for every administrator.
- Internet access to Telegram, Ollama model storage, and monitored websites.

Python, Ollama, and project dependencies do not need to be installed separately.

## CPU Start

From the project directory, run:

```bash
docker compose up -d --build
```

On the first start, the `model-init` container downloads `qwen3:8b`. Follow the download:

```bash
docker compose logs -f model-init
```

Follow bot logs:

```bash
docker compose logs -f bot
```

Stop the stack:

```bash
docker compose down
```

The model and application data are stored in Docker volumes and survive a normal `docker compose down`.

## NVIDIA GPU Start

On Linux, install a working NVIDIA driver and NVIDIA Container Toolkit. On Windows, use Docker Desktop with the WSL 2 backend and an up-to-date NVIDIA driver.

Run:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Check the active Ollama processor:

```bash
docker compose exec ollama ollama ps
```

## Windows Quick Start

Enable and update WSL 2 from an administrator PowerShell window:

```powershell
wsl --install
wsl --update
wsl --set-default-version 2
```

Restart Windows, install Docker Desktop, select the WSL 2 backend, and start Docker Desktop.

Extract the archive:

```powershell
New-Item -ItemType Directory -Force "C:\AI" | Out-Null

Expand-Archive `
  -Path "$env:USERPROFILE\Downloads\ollama_news_bot_v2.zip" `
  -DestinationPath "C:\AI" `
  -Force

cd "C:\AI\ollama_news_bot"
```

Start on CPU:

```powershell
docker compose up -d --build
```

Start with an NVIDIA GPU:

```powershell
docker compose `
  -f docker-compose.yml `
  -f docker-compose.gpu.yml `
  up -d --build
```

Docker Desktop must remain running. If the computer sleeps or shuts down, the bot stops.

## Telegram Control

After startup, send `/start` to the control bot.

The main menu contains:

- `Sources` — add any public HTTP/HTTPS URL, enable, disable, remove, or manually check it.
- `Interval` — set the background polling period using `300`, `30s`, `5m`, or `1h`.
- `Prompts` — fully edit discovery, extraction, and processing prompts.
- `Export and channel` — configure CSV, Telegram publication, channel, publisher token, and report download.
- `Status` — view checks, discovered URLs, and errors.
- `Check now` — immediately run all enabled sources.
- `Pause` — pause background monitoring for the current user.
- `Users` — administrator-only user management.

The `/cancel` command cancels the current input operation. The `/admin` command opens user management for administrators.

## Telegram Channel Publication

A Telegram channel does not have its own token. Configure:

1. A channel ID such as `-1001234567890` or a public name such as `@channel`.
2. The control bot as a channel administrator; or
3. A separate publisher bot token whose bot is already a channel administrator.

Text exceeding Telegram's message limit is split automatically.

## Processing Pipeline

1. The bot fetches the page containing the general publication stream.
2. It removes JavaScript, styles, navigation, headers, footers, advertisements, and technical noise.
3. It compacts the DOM into semantic link blocks with surrounding context.
4. The discovery prompt selects the latest item URL from the primary stream.
5. The URL is compared with the saved source state and the user's processed history.
6. The detail page is fetched and cleaned separately.
7. The extraction prompt returns structured article data through JSON Schema.
8. The processing prompt creates the final output.
9. The result is independently written to CSV and/or published to Telegram.

By default, the first discovered link becomes the source baseline and is not published. This prevents old content from being published immediately after a source is added. Enable `Process first item` to change this behavior. A manual check also forces processing.

## Data Files

Inside the `bot_data` volume:

```text
/app/data/users.json
/app/data/exports/user_<telegram_id>_articles.csv
```

The JSON database stores user profiles, sources, prompts, output settings, source state, and a limited history of processed items. Writes use a temporary file followed by an atomic replacement.

## Environment Configuration

Edit `.env` before starting the stack.

Main settings:

- `TELEGRAM_BOT_TOKEN` — control bot token.
- `ADMIN_IDS` — one or more numeric Telegram IDs separated by commas.
- `OLLAMA_MODEL` — Ollama model name.
- `OLLAMA_NUM_CTX` — model context size.
- `MAX_CONCURRENT_JOBS` — maximum number of concurrent AI pipelines.
- `DEFAULT_INTERVAL_SECONDS` — default interval for a new user.
- `MIN_INTERVAL_SECONDS` — minimum allowed interval.
- `ALLOW_EXTERNAL_ARTICLE_URLS` — whether discovery may select another domain.
- `NOTIFY_SUCCESS` and `NOTIFY_ERRORS` — control-bot notifications.

After changing the model, run:

```bash
docker compose run --rm model-init
docker compose restart bot
```

If another application instance uses the same Telegram bot token, stop it before starting this project. Telegram long polling allows only one active polling process per bot token.

## Local Validation Without Docker

Linux or macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check app tests main.py
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest -q
ruff check app tests main.py
```

For direct Python execution, Ollama must be available at `OLLAMA_URL`, and `DATA_DIR` must point to a writable directory.

## Fetcher Limitations

Browser-mode fallback handles some ordinary anti-bot pages, but it does not solve interactive CAPTCHA, authentication, or closed content. Such responses are recorded as source errors. Repeated failures use exponential backoff up to one hour to avoid unnecessary load on the website.

The current discovery stage returns one latest URL per source check. If several items can be published between checks, use a sufficiently short interval or extend discovery to return a list of recent URLs and compare it with processed history.
