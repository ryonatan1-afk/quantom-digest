# Quantum Digest

Autonomous daily agent that searches the web for quantum computing news and delivers a digest to Telegram every morning at 07:00 Israel time.

## What it does

- Searches for quantum computing news from the last 48 hours (new companies, milestones, research, funding)
- Deduplicates against the last 7 days so you never see the same story twice
- Sends one Telegram message per day grouped by category
- Logs every item to `items.csv` in this repo

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/ryonatan1-afk/quantom-digest
cd quantom-digest
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create a `.env` file

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
```

To find your Telegram chat ID: message [@userinfobot](https://t.me/userinfobot) on Telegram.

### 4. Run locally

```bash
python quantum_watch.py
```

## GitHub Actions (automatic daily runs)

The workflow in `.github/workflows/daily.yml` runs at 04:00 UTC (07:00 Israel summer time) every day.

### Add repository secrets

Go to your repo on GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**, and add:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |

After each run, the workflow automatically commits the updated `items.csv` back to the repo so deduplication persists across days.

### Manual trigger

You can trigger a run at any time: go to **Actions** → **Daily Quantum Digest** → **Run workflow**.

## CSV format

`items.csv` columns: `date_reported`, `title`, `summary`, `url`, `category`

Categories: `company`, `milestone`, `research`, `funding`, `other`
