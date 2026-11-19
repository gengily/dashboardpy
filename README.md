# RegiBot Hub

A lightweight, self-hosted dashboard for RegiBot catch reports. Tracks accounts, catches, shinies, hundos, and inventory using Python and SQLite.


## Preview
https://gengily.github.io/dashboardpy/


## Features

- Account stats, collections, and recent catches
- Searchable inventory with trade tracking and undo
- Live updates; only caught events are saved
- Prevents duplicate Discord messages

## Setup

Requires Python 3.9+.

```bash
git clone https://github.com/gengily/dashboardpy.git
cd dashboardpy
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications). Enable **Message Content Intent**, then invite it with **View Channels** and **Read Message History** permissions. Copy the bot token and the ID of RegiBot’s channel.

## Run

Set your environment variables and start the app:

```bash
export SECRET="use-a-long-random-string"
export DISCORD_TOKEN="your-bot-token"
export CHANNEL_ID="your-channel-id"
python webhook_hub.py
```

Open `http://localhost:8080/?key=YOUR_SECRET`.

The hub starts collecting from the current time. Set `BACKFILL=1` to import existing channel history.

## Dashboard

- **Accounts:** Trainer stats, catches, and collections
- **Inventory:** Pokémon grouped by species, with search and trade controls
- **Filters:** All, Shiny, Hundo, or Background
- **Appearance:** Choose a theme and accent color

Accounts ending in five or more digits are hidden by default. Set `SHOW_OLD=1` to show them.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `SECRET` | `change-me` | Dashboard access key. Change this. |
| `DISCORD_TOKEN` | — | Discord bot token |
| `CHANNEL_ID` | — | RegiBot’s channel ID |
| `BACKFILL` | off | Import existing messages when set to `1` |
| `POLL_SECS` | `4` | Discord polling interval |
| `DB` | `hub.db` | SQLite database path |
| `PORT` | `8080` | HTTP port |
| `SHOW_OLD` | off | Show all accounts when set to `1` |

Alternatively, configure RegiBot to send directly to:

`http://YOUR_HOST:8080/api/webhooks/anything/YOUR_SECRET`

Use either the webhook or the Discord bot, not both.

## Stop or Shut Down

- **Running in a terminal:** Press `Ctrl+C`.
- **Running as a systemd service:** Stop it with:
  `sudo systemctl stop regibot-hub`
- **Prevent it from starting at boot:**
  `sudo systemctl disable regibot-hub`
- **Stop data collection:** Stop the app and remove its bot from the server, or disable the webhook in RegiBot.

Replace `regibot-hub` with your actual service name if it differs.

## Backups and Security

Back up `hub.db` to preserve your data. Use HTTPS or a VPN before exposing the dashboard; the access key is included in the URL. Never commit tokens or secrets.

## License and Contact

See the project repository for license and contact details:

https://github.com/gengily/dashboardpy
