# RegiBot Hub

A small self-hosted dashboard that collects [RegiBot](https://github.com/) catch reports from a Discord channel
and shows every account, catch, shiny, hundo and your current inventory in one place. Single Python file, SQLite database, no build step.

- **Accounts** view: per-trainer stats, collection grid and recent catches
- **Inventory** view: everything you currently hold, grouped by species, with search and a "Traded away" button
- Live updates without page reloads
- Only **caught** events are stored. "Encountered" messages are ignored.
- Every Discord message is stored **once** (by message id), so restarts or re-polls never create duplicates

## 1. Install

You need Python 3.9+.

```bash
git clone https://github.com/gengily/dashboardpy.git
cd dashboardpy
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Create the Discord bot

RegiBot posts to a normal Discord webhook. The hub reads that channel with a bot of your own.

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Go to **Bot**, click **Reset Token** and copy the token. Keep it secret.
3. On the same page enable **Message Content Intent** (without it the bot sees messages without their embeds).
4. Go to **OAuth2 > URL Generator**, tick the scope `bot`, then the permissions **View Channels** and **Read Message History**.
   Open the generated URL and add the bot to your server.
5. In Discord enable **Settings > Advanced > Developer Mode**, right-click the channel RegiBot posts in and choose **Copy Channel ID**.

## 3. Run it

```bash
export SECRET="a-long-random-string"
export DISCORD_TOKEN="your bot token"
export CHANNEL_ID="your channel id"
python webhook_hub.py
```

Windows PowerShell:

```powershell
$env:SECRET="a-long-random-string"; $env:DISCORD_TOKEN="..."; $env:CHANNEL_ID="..."; python webhook_hub.py
```

Open `http://localhost:8080/?key=a-long-random-string`.

By default the hub starts **from now** and ignores older channel history. To import the history once, start it with `BACKFILL=1`.

## 4. Using the dashboard

| Where | What it does |
| --- | --- |
| **Accounts** tab | Totals, recent catches and one card per trainer. Tap a trainer for their collection. |
| **Inventory** tab | Everything held, grouped by species. Expand a card to see each Pokémon. |
| **Traded away** | Marks one Pokémon as traded. It leaves your counts. Use **Undo** or the **Show traded** toggle to bring it back. |
| **Palette button** (top right) | Pick **Auto / Light / Dark** and an accent color (Ocean, Rose, Forest, Violet, Sunset). Saved in your browser. |
| Filters | All / Shiny / Hundo / Background |
| Search | Pokémon name or account name (Inventory) |

Only accounts whose name ends in 1 to 4 digits are shown (for example `Ash1234`). Names with 5 or more trailing digits are treated as old accounts and hidden. Set `SHOW_OLD=1` to show everything.

## 5. Settings

All settings are environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SECRET` | `change-me` | Password for the dashboard (`?key=...`). **Change it.** |
| `DISCORD_TOKEN` | none | Bot token. Without it only the webhook endpoint runs. |
| `CHANNEL_ID` | none | Channel RegiBot posts in. |
| `BACKFILL` | off | `1` imports the existing channel history on first start. |
| `POLL_SECS` | `4` | How often Discord is polled. |
| `DB` | `hub.db` | SQLite file path. |
| `PORT` | `8080` | HTTP port. |
| `SHOW_OLD` | off | `1` disables the old-account filter. |
| `SOURCE_URL` | `https://github.com/gengily/dashboardpy` | Repository link shown in the footer. Forks must point this at their own source (see License). |
| `DISCORD_URL` | `https://discord.gg/Y72EpmWj9M` | Discord invite shown in the footer. |

### Optional: direct webhook instead of the bot

If your RegiBot version accepts a custom webhook URL you can point it at
`http://YOUR_HOST:8080/api/webhooks/anything/<SECRET>` and skip the Discord bot. Use one method or the other, not both,
otherwise the same catch arrives twice with different ids.

## 6. Running it permanently

- Put it behind HTTPS (Caddy or nginx reverse proxy) or a VPN such as Tailscale. The secret travels in the URL, so do not expose it over plain HTTP on the internet.
- Example systemd unit:

```ini
[Unit]
Description=RegiBot Hub
After=network-online.target

[Service]
WorkingDirectory=/opt/regibot-hub
EnvironmentFile=/opt/regibot-hub/.env
ExecStart=/opt/regibot-hub/.venv/bin/python webhook_hub.py
Restart=always

[Install]
WantedBy=multi-user.target
```

`.env` contains lines like `SECRET=...`, `DISCORD_TOKEN=...`, `CHANNEL_ID=...`. Never commit it.

## 7. Backups and cleanup

Everything lives in `hub.db`. Copy that file to back up. Upgrading from an older version needs no action, the database is migrated on start.

Old "encountered" rows from earlier versions are already hidden everywhere. To delete them for good:

```bash
sqlite3 hub.db "DELETE FROM events WHERE event='encountered';"
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Log says messages had **NO embeds** | Enable **Message Content Intent** in the Developer Portal. |
| `HTTP 403` / `404` in the log | Wrong channel id, or the bot cannot see that channel. |
| Dashboard shows nothing | Check the account name rule above, or set `SHOW_OLD=1`. Without `BACKFILL=1` old messages are skipped. |
| Page returns 404 | The `?key=` in the URL does not match `SECRET`. |

## Community

- Discord: https://discord.gg/Y72EpmWj9M
- GitHub: https://github.com/gengily/dashboardpy

## License

Copyright (C) 2026 gengily

Licensed under the **GNU Affero General Public License v3.0 or later** (AGPL-3.0-or-later), see the `LICENSE` file.

- You may use, modify and share it freely.
- You must keep the copyright and license notices, so the original author stays credited.
- If you publish a modified version, or let other people use it over a network, you must release your source under the same license and offer it to those users (set `SOURCE_URL` to your fork).
