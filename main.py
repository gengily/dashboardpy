# SPDX-License-Identifier: AGPL-3.0-or-later
# RegiBot Hub - Copyright (C) 2026 gengily
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License v3 or later. See the LICENSE file.
"""
webhook_hub.py - receives RegiBot (Discord-style) webhooks, tracks each account.

Run:   pip install flask
       SECRET=longrandomstring DISCORD_TOKEN=... CHANNEL_ID=... python webhook_hub.py

Two ways to feed it (use either or both):
  A) Discord bot (recommended): RegiBot posts to a REAL Discord webhook; this
     script logs in as a bot, reads that channel, and records every embed.
     Needs DISCORD_TOKEN + CHANNEL_ID (bot needs "Message Content Intent").
     Optional: BACKFILL=1 imports the channel history on first run.
  B) Direct webhook (only if the bot accepts custom URLs):
       http://YOUR_HOST:8080/api/webhooks/<id>/<SECRET>

Dashboard:
       http://YOUR_HOST:8080/?key=<SECRET>
Only CAUGHT events are stored. "Encountered" messages are ignored, and every Discord
message id is stored once, so the same message can never be counted twice.
(Older hub.db files are migrated automatically.)
"""
import json, os, re, sqlite3, threading, time, urllib.error, urllib.request
from collections import Counter
from datetime import datetime
from flask import Flask, request, abort, jsonify, render_template_string

SECRET = os.environ.get("SECRET", "change-me")
DB = os.environ.get("DB", "hub.db")
SOURCE_URL = os.environ.get("SOURCE_URL", "https://github.com/gengily/dashboardpy")  # AGPL: link to your repo, shown in the footer
DISCORD_URL = os.environ.get("DISCORD_URL", "https://discord.gg/Y72EpmWj9M")
SHOW_OLD = os.environ.get("SHOW_OLD") == "1"  # set to 1 to disable the old-account filter


def valid_account(name):
    """Real accounts end in 1-4 digits. 5+ trailing digits = old account.
    Names with no trailing digits (e.g. 'unknown') are treated as not real."""
    if SHOW_OLD:
        return 1
    m = re.search(r"\d+$", name or "")
    return int(bool(m) and len(m.group()) <= 4)
app = Flask(__name__)
app.jinja_env.globals["source_url"] = SOURCE_URL
app.jinja_env.globals["discord_url"] = DISCORD_URL


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.create_function("ok", 1, valid_account)
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, ts REAL, account TEXT, event TEXT,
        pokemon TEXT, shiny INTEGER, iv TEXT, iv_pct INTEGER, cp INTEGER,
        mode TEXT, lat REAL, lon REAL, total_shinies INTEGER, thumb TEXT, raw TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
    if "traded" not in cols:  # migrate older databases
        c.execute("ALTER TABLE events ADD COLUMN traded INTEGER DEFAULT 0")
    if "traded_ts" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN traded_ts REAL")
    if "background" not in cols:
        try:
            c.execute("ALTER TABLE events ADD COLUMN background INTEGER DEFAULT 0")
            for r in c.execute("SELECT id, raw FROM events").fetchall():
                try:
                    bg = extract(json.loads(r["raw"]))["background"]
                except Exception:
                    bg = 0
                if bg:
                    c.execute("UPDATE events SET background=1 WHERE id=?", (r["id"],))
            c.commit()
        except sqlite3.OperationalError:
            pass  # another thread already migrated
    if "msg_id" not in cols:  # Discord message id: lets us refuse a message we already saw
        try:
            c.execute("ALTER TABLE events ADD COLUMN msg_id TEXT")
        except sqlite3.OperationalError:
            pass  # another thread already added it
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_events_msg ON events(msg_id)")
    return c


def extract(payload):
    """Parse a RegiBot embed into a flat dict."""
    embeds = payload.get("embeds") or [{}]
    e = embeds[0]
    title = (e.get("title") or payload.get("content") or "").strip()
    f = {(x.get("name") or "").strip().lower(): (x.get("value") or "").strip()
         for x in e.get("fields", [])}

    event, name = "other", title
    m = re.match(r"^(Encountered|Caught):\s*(.+)$", title, re.I)
    if m:
        event, name = m.group(1).lower(), m.group(2)
    else:
        m = re.match(r"^\W*Shiny\s+(.+?)\s+caught!?$", title, re.I)
        if m:
            event, name = "caught", "Shiny " + m.group(1)

    shiny = int("✨" in title or bool(re.search(r"\bshiny\b", name, re.I)))
    pokemon = re.sub(r"✨|\bShiny\b", "", name, flags=re.I).strip()
    pokemon = re.sub(r"\s+", " ", pokemon)

    iv, iv_pct = f.get("iv"), None
    if iv:
        m = re.search(r"\((\d+)%\)", iv)
        iv_pct = int(m.group(1)) if m else None

    cp = None
    if re.search(r"\d+", f.get("cp", "")):
        cp = int(re.search(r"\d+", f["cp"]).group())

    lat = lon = None
    m = re.search(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", f.get("location", ""))
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))

    ts_ = None
    if re.search(r"\d+", f.get("total shinies", "")):
        ts_ = int(re.search(r"\d+", f["total shinies"]).group())

    bg = int(bool(re.search(r"\byes\b", f.get("background", ""), re.I)))

    thumb = (e.get("thumbnail") or {}).get("url") or (e.get("image") or {}).get("url")

    return dict(account=f.get("trainer") or payload.get("username") or "unknown",
                event=event, pokemon=pokemon, shiny=shiny, iv=iv, iv_pct=iv_pct,
                cp=cp, mode=f.get("mode"), lat=lat, lon=lon, total_shinies=ts_,
                thumb=thumb, background=bg)


def store(payload, ts=None, msg_id=None):
    """Save one payload. Returns "saved", "duplicate" or "skipped".
    - encountered events are never stored (only catches matter)
    - a message id that is already in the database is never stored again"""
    d = extract(payload)
    if d["event"] == "encountered":
        return "skipped"
    with db() as c:
        cur = c.execute("""INSERT OR IGNORE INTO events(ts,account,event,pokemon,shiny,iv,iv_pct,cp,
                     mode,lat,lon,total_shinies,thumb,background,raw,msg_id)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ts or time.time(), d["account"], d["event"], d["pokemon"], d["shiny"],
                         d["iv"], d["iv_pct"], d["cp"], d["mode"], d["lat"], d["lon"],
                         d["total_shinies"], d["thumb"], d["background"], json.dumps(payload),
                         str(msg_id) if msg_id else None))
    return "saved" if cur.rowcount else "duplicate"


@app.post("/api/webhooks/<wid>/<token>")
def receive(wid, token):
    if token != SECRET:
        abort(404)
    body = request.get_json(silent=True) or {}
    store(body, msg_id=body.get("id"))
    return "", 204


# ---------------- Discord channel poller (bot) ----------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
POLL_SECS = float(os.environ.get("POLL_SECS", 4))
BACKFILL = os.environ.get("BACKFILL") == "1"


def discord_get(path):
    req = urllib.request.Request("https://discord.com/api/v10" + path, headers={
        "Authorization": "Bot " + DISCORD_TOKEN,
        "User-Agent": "DiscordBot (regibot-hub, 1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get_cursor():
    with db() as c:
        r = c.execute("SELECT v FROM meta WHERE k='cursor'").fetchone()
    return r["v"] if r else None


def set_cursor(v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('cursor',?)", (v,))


def poll_discord():
    print(f"[discord] polling channel {CHANNEL_ID} every {POLL_SECS}s")
    cursor = get_cursor()
    while True:
        try:
            if cursor is None:
                if BACKFILL:
                    cursor = "0"
                else:  # start from now, don't import old history
                    newest = discord_get(f"/channels/{CHANNEL_ID}/messages?limit=1")
                    cursor = newest[0]["id"] if newest else "0"
                    set_cursor(cursor)
                    print(f"[discord] starting fresh after message {cursor}: anything "
                          "posted BEFORE now is ignored (use BACKFILL=1 to import it)")
            msgs = discord_get(f"/channels/{CHANNEL_ID}/messages?limit=100&after={cursor}")
            res = Counter()
            for m in sorted(msgs, key=lambda m: int(m["id"])):
                if int(m["id"]) <= int(cursor):
                    continue  # already handled, never take a message twice
                if m.get("embeds"):
                    res["embeds"] += 1
                    res[store({"embeds": m["embeds"], "content": m.get("content"),
                               "username": (m.get("author") or {}).get("username")},
                              ts=datetime.fromisoformat(m["timestamp"]).timestamp(),
                              msg_id=m["id"])] += 1
                cursor = m["id"]
                set_cursor(cursor)
            if msgs:
                print(f"[discord] {len(msgs)} new message(s): {res['saved']} saved, "
                      f"{res['skipped']} encountered ignored, {res['duplicate']} duplicate")
                if not res["embeds"]:
                    print("[discord] messages had NO embeds - the bot probably lacks "
                          "'Message Content Intent' (Developer Portal > Bot), "
                          "or these weren't RegiBot embeds")
            if len(msgs) >= 100:
                continue  # more history waiting
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 5))
                except Exception:
                    wait = 5
                time.sleep(wait + 0.5)
                continue
            print(f"[discord] HTTP {e.code}: {body}")
            if e.code in (401, 403, 404):
                print("[discord] check the token, channel ID and bot permissions")
                time.sleep(30)
        except Exception as e:
            print("[discord] error:", e)
        time.sleep(POLL_SECS)


PAGE = r"""
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f6f9ff">
<title>RegiBot Hub</title>
<script>
/* apply the saved theme before first paint so the page never flashes the wrong colours */
(function(){try{var d=document.documentElement,m=localStorage.getItem('hub-mode'),a=localStorage.getItem('hub-accent');
if(m==='light'||m==='dark')d.dataset.mode=m;if(a&&a!=='ocean')d.dataset.accent=a;}catch(e){}})();
</script>
<style>
/* light */
:root{color-scheme:light dark;--bg:#f6f9ff;--surface:#e9eff8;--surface-hi:#dae3f0;--on-surface:#171c23;--on-var:#414856;--outline:#bcc6d6;--p:#00629d;--on-p:#fff;--pc:#9ed5ff;--on-pc:#001d33;--s:#4e6076;--sc:#d1e2f6;--on-sc:#0a1d2f;--t:#7a4f8d;--tc:#f1d8ff;--on-tc:#2f0f40;--gold:#ffdf8e;--on-gold:#3f2e00;
  --spring:cubic-bezier(.34,1.56,.64,1);
  --font:"Google Sans Flex","Google Sans",Roboto,system-ui,-apple-system,"Segoe UI",sans-serif}
:root[data-mode=light]{color-scheme:light}
:root[data-mode=dark]{color-scheme:dark}
:root[data-accent=rose]{--bg:#fff8f8;--surface:#f8eaec;--surface-hi:#f0dadd;--on-surface:#22191b;--on-var:#524345;--outline:#d5c1c4;--p:#a8315a;--on-p:#fff;--pc:#ffd9e2;--on-pc:#3e001d;--s:#75565d;--sc:#ffd9e0;--on-sc:#2b151a;--t:#7b5733;--tc:#ffddb9;--on-tc:#2b1700;--gold:#ffdf8e;--on-gold:#3f2e00}
:root[data-accent=forest]{--bg:#f6fbf4;--surface:#e8f1e6;--surface-hi:#d8e6d6;--on-surface:#171d17;--on-var:#414942;--outline:#bccabb;--p:#1f6a3b;--on-p:#fff;--pc:#a8f2b8;--on-pc:#00210d;--s:#52634f;--sc:#d5e8d0;--on-sc:#101f10;--t:#39656b;--tc:#bcebf2;--on-tc:#001f23;--gold:#ffdf8e;--on-gold:#3f2e00}
:root[data-accent=violet]{--bg:#fbf8ff;--surface:#efe9f8;--surface-hi:#e2daef;--on-surface:#1c1a22;--on-var:#494554;--outline:#cbc3d8;--p:#6445b6;--on-p:#fff;--pc:#e6deff;--on-pc:#1f0a5a;--s:#625b71;--sc:#e8def8;--on-sc:#1d192b;--t:#7e5260;--tc:#ffd8e4;--on-tc:#31111d;--gold:#ffdf8e;--on-gold:#3f2e00}
:root[data-accent=sunset]{--bg:#fff8f4;--surface:#f7ebe2;--surface-hi:#eddccf;--on-surface:#211a14;--on-var:#51443a;--outline:#d6c3b4;--p:#9a4600;--on-p:#fff;--pc:#ffdcc6;--on-pc:#321300;--s:#765846;--sc:#f9dfcb;--on-sc:#2b1709;--t:#5f6219;--tc:#e4e78f;--on-tc:#1c1d00;--gold:#ffdf8e;--on-gold:#3f2e00}
/* dark: follows the system unless a mode is chosen */
@media (prefers-color-scheme: dark){:root:not([data-mode=light]){--bg:#1a191f;--surface:#252a30;--surface-hi:#3b4858;--on-surface:#e4e2ea;--on-var:#c1c7d2;--outline:#4a5463;--p:#9ed5ff;--on-p:#00344f;--pc:#1f4d72;--on-pc:#cfe8ff;--s:#b9c9dc;--sc:#293645;--on-sc:#d3e4f8;--t:#dcbce9;--tc:#533c63;--on-tc:#f6dcff;--gold:#5f4a00;--on-gold:#ffe28f}
:root:not([data-mode=light])[data-accent=rose]{--bg:#1d1517;--surface:#2a2023;--surface-hi:#3e3135;--on-surface:#ede0e1;--on-var:#d6c2c4;--outline:#594a4d;--p:#ffb1c8;--on-p:#650032;--pc:#8b1743;--on-pc:#ffd9e2;--s:#e5bdc6;--sc:#4a2f36;--on-sc:#ffd9e0;--t:#e9bf94;--tc:#5a4020;--on-tc:#ffddb9;--gold:#5f4a00;--on-gold:#ffe28f}
:root:not([data-mode=light])[data-accent=forest]{--bg:#131a14;--surface:#1f2820;--surface-hi:#313d32;--on-surface:#dfe4dc;--on-var:#c0c9bf;--outline:#4a5a4c;--p:#8dd69e;--on-p:#00391b;--pc:#145229;--on-pc:#a8f2b8;--s:#b9ccb4;--sc:#3b4b39;--on-sc:#d5e8d0;--t:#a1ced5;--tc:#1f4d54;--on-tc:#bcebf2;--gold:#5f4a00;--on-gold:#ffe28f}
:root:not([data-mode=light])[data-accent=violet]{--bg:#16131c;--surface:#231f2b;--surface-hi:#382f46;--on-surface:#e6e0e9;--on-var:#cbc4d2;--outline:#4f4860;--p:#cabeff;--on-p:#32128a;--pc:#4a2f9d;--on-pc:#e6deff;--s:#ccc2dc;--sc:#4a4458;--on-sc:#e8def8;--t:#efb8c8;--tc:#633b48;--on-tc:#ffd8e4;--gold:#5f4a00;--on-gold:#ffe28f}
:root:not([data-mode=light])[data-accent=sunset]{--bg:#1a1410;--surface:#281f19;--surface-hi:#3d3027;--on-surface:#ede0d8;--on-var:#d5c3b8;--outline:#5a4a3e;--p:#ffb68a;--on-p:#532200;--pc:#763300;--on-pc:#ffdcc6;--s:#e5bfa8;--sc:#5a4130;--on-sc:#ffdcc6;--t:#c8cb76;--tc:#484a00;--on-tc:#e4e78f;--gold:#5f4a00;--on-gold:#ffe28f}}
:root[data-mode=dark]{--bg:#1a191f;--surface:#252a30;--surface-hi:#3b4858;--on-surface:#e4e2ea;--on-var:#c1c7d2;--outline:#4a5463;--p:#9ed5ff;--on-p:#00344f;--pc:#1f4d72;--on-pc:#cfe8ff;--s:#b9c9dc;--sc:#293645;--on-sc:#d3e4f8;--t:#dcbce9;--tc:#533c63;--on-tc:#f6dcff;--gold:#5f4a00;--on-gold:#ffe28f}
:root[data-mode=dark][data-accent=rose]{--bg:#1d1517;--surface:#2a2023;--surface-hi:#3e3135;--on-surface:#ede0e1;--on-var:#d6c2c4;--outline:#594a4d;--p:#ffb1c8;--on-p:#650032;--pc:#8b1743;--on-pc:#ffd9e2;--s:#e5bdc6;--sc:#4a2f36;--on-sc:#ffd9e0;--t:#e9bf94;--tc:#5a4020;--on-tc:#ffddb9;--gold:#5f4a00;--on-gold:#ffe28f}
:root[data-mode=dark][data-accent=forest]{--bg:#131a14;--surface:#1f2820;--surface-hi:#313d32;--on-surface:#dfe4dc;--on-var:#c0c9bf;--outline:#4a5a4c;--p:#8dd69e;--on-p:#00391b;--pc:#145229;--on-pc:#a8f2b8;--s:#b9ccb4;--sc:#3b4b39;--on-sc:#d5e8d0;--t:#a1ced5;--tc:#1f4d54;--on-tc:#bcebf2;--gold:#5f4a00;--on-gold:#ffe28f}
:root[data-mode=dark][data-accent=violet]{--bg:#16131c;--surface:#231f2b;--surface-hi:#382f46;--on-surface:#e6e0e9;--on-var:#cbc4d2;--outline:#4f4860;--p:#cabeff;--on-p:#32128a;--pc:#4a2f9d;--on-pc:#e6deff;--s:#ccc2dc;--sc:#4a4458;--on-sc:#e8def8;--t:#efb8c8;--tc:#633b48;--on-tc:#ffd8e4;--gold:#5f4a00;--on-gold:#ffe28f}
:root[data-mode=dark][data-accent=sunset]{--bg:#1a1410;--surface:#281f19;--surface-hi:#3d3027;--on-surface:#ede0d8;--on-var:#d5c3b8;--outline:#5a4a3e;--p:#ffb68a;--on-p:#532200;--pc:#763300;--on-pc:#ffdcc6;--s:#e5bfa8;--sc:#5a4130;--on-sc:#ffdcc6;--t:#c8cb76;--tc:#484a00;--on-tc:#e4e78f;--gold:#5f4a00;--on-gold:#ffe28f}
*{box-sizing:border-box;margin:0}
html{-webkit-tap-highlight-color:transparent}
body{font-family:var(--font);background:var(--bg);color:var(--on-surface);
  padding:env(safe-area-inset-top) 0 calc(40px + env(safe-area-inset-bottom));}
a{color:inherit;text-decoration:none}
main{max-width:860px;margin:0 auto;padding:0 16px;overflow-x:clip}

/* ---------- top bar ---------- */
.top{display:flex;align-items:center;gap:12px;padding:20px 0 8px}
.top .grow{flex:1;min-width:0}
.eyebrow{font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--p)}
h1{font-size:clamp(38px,10vw,60px);line-height:1;font-weight:800;letter-spacing:-.03em;
  overflow-wrap:anywhere;margin-top:4px}
.icon-btn{width:52px;height:52px;display:grid;place-items:center;flex:none;border:0;cursor:pointer;
  font:700 22px var(--font);background:var(--sc);color:var(--on-sc);border-radius:50%;
  transition:border-radius .35s var(--spring),transform .35s var(--spring),background .2s}
.icon-btn:hover{border-radius:18px}
.icon-btn:active{transform:scale(.88);border-radius:14px}
.icon-btn.spin:active{transform:rotate(180deg) scale(.9)}

/* ---------- stat bento ---------- */
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:18px 0 8px}
@media(min-width:640px){.stats{grid-template-columns:repeat(4,1fr)}}
.stat{padding:18px 20px 16px;min-height:118px;display:flex;flex-direction:column;justify-content:space-between;
  transition:transform .4s var(--spring),border-radius .4s var(--spring)}
.stat:active{transform:scale(.96)}
.stat b{font-size:clamp(38px,9vw,52px);line-height:1;font-weight:800;letter-spacing:-.03em}
.stat>span{font-size:14px;font-weight:600;opacity:.85}
.stat b.num{display:flex;align-items:center;gap:.14em}
.stat b.num .ic{width:.8em;height:.8em}
.stat:nth-child(1){background:var(--pc);color:var(--on-pc);border-radius:44px 14px 14px 14px}
.stat:nth-child(2){background:var(--sc);color:var(--on-sc);border-radius:14px 44px 14px 14px}
.stat:nth-child(3){background:var(--gold);color:var(--on-gold);border-radius:14px 14px 14px 44px}
.stat:nth-child(4){background:var(--tc);color:var(--on-tc);border-radius:14px 14px 44px 14px}
.stat:hover{border-radius:36px}

/* ---------- section headings + segmented filter ---------- */
.sec{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:34px 0 12px}
h2{font-size:26px;font-weight:700;letter-spacing:-.01em}
.seg{display:inline-flex;gap:2px;padding:3px;background:var(--surface-hi);border-radius:999px}
.seg a{display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:999px;font-size:14px;font-weight:600;color:var(--on-var);
  transition:all .35s var(--spring)}
.seg a.on{background:var(--p);color:var(--on-p);padding:9px 20px}
.seg a:active{transform:scale(.92)}

/* ---------- trainer cards ---------- */
.trainers{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(290px,1fr))}
.trainer{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;align-items:center;padding:18px;
  background:var(--surface);border-radius:32px;
  transition:transform .45s var(--spring),border-radius .45s var(--spring),background .2s}
.trainer:hover{background:var(--surface-hi);border-radius:44px 22px 44px 22px}
.trainer:active{transform:scale(.95)}
.avatar{width:64px;height:64px;display:grid;place-items:center;font-size:26px;font-weight:800;
  transition:border-radius .6s var(--spring),transform .6s var(--spring)}
.trainer:hover .avatar{transform:rotate(-8deg) scale(1.08)}
.sh0{border-radius:50%;background:var(--pc);color:var(--on-pc)}
.sh1{border-radius:30%;background:var(--tc);color:var(--on-tc)}
.sh2{border-radius:50% 8% 50% 8%;background:var(--sc);color:var(--on-sc)}
.sh3{border-radius:62% 38% 46% 54% / 60% 40% 60% 40%;background:var(--gold);color:var(--on-gold)}
.trainer:hover .sh0{border-radius:34%}
.trainer:hover .sh1{border-radius:50%}
.trainer:hover .sh2{border-radius:8% 50% 8% 50%}
.trainer:hover .sh3{border-radius:40% 60% 54% 46% / 40% 60% 40% 60%}
.t-name{font-size:19px;font-weight:700;overflow-wrap:anywhere}
.t-last{font-size:13px;color:var(--on-var)}
.t-chips{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}

/* ---------- chips ---------- */
.chip{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:999px;font-size:13px;
  font-weight:600;background:var(--surface-hi);color:var(--on-var);white-space:nowrap}
.chip.shiny{background:var(--gold);color:var(--on-gold)}
.chip.hundo{background:var(--tc);color:var(--on-tc)}
.chip.p{background:var(--pc);color:var(--on-pc)}
a.chip{transition:transform .35s var(--spring)}
a.chip:active{transform:scale(.9)}

/* ---------- collection grid ---------- */
.dex{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(118px,1fr))}
.mon{display:flex;flex-direction:column;align-items:center;text-align:center;gap:2px;padding:14px 8px 12px;
  background:var(--surface);border-radius:28px;
  transition:transform .45s var(--spring),border-radius .45s var(--spring)}
.mon:hover{border-radius:40px 16px 40px 16px;transform:translateY(-3px)}
.mon.is-shiny{background:var(--gold);color:var(--on-gold)}
.mon img,.mon .ph{width:68px;height:68px;object-fit:contain}
.mon b{font-size:14px;line-height:1.15;margin-top:4px}
.mon small{font-size:12px;opacity:.8;font-weight:600}
.ph{display:grid;place-items:center;font-size:26px;font-weight:800;background:var(--sc);color:var(--on-sc);border-radius:50%}

/* ---------- catch list ---------- */
.catches{list-style:none;padding:0;display:grid;gap:6px}
.catch{display:flex;gap:14px;align-items:center;padding:12px 16px 12px 12px;background:var(--surface);
  border-radius:28px;transition:transform .4s var(--spring),border-radius .4s var(--spring)}
.catch:first-child{border-radius:36px 36px 12px 12px}
.catch:last-child{border-radius:12px 12px 36px 36px}
.catch:only-child{border-radius:36px}
.catch:active{transform:scale(.97)}
.catch.is-shiny{background:linear-gradient(100deg,var(--surface) 55%,var(--gold))}
.thumb{width:68px;height:68px;flex:none;display:grid;place-items:center;background:var(--surface-hi);
  border-radius:24px 24px 24px 10px;overflow:hidden;font-weight:800;font-size:24px;color:var(--p)}
.thumb img{width:100%;height:100%;object-fit:contain}
.catch .meta{min-width:0;display:grid;gap:4px}
.c-title{font-size:17px;font-weight:700;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.c-sub{font-size:13px;color:var(--on-var)}
.c-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}

.empty{padding:36px 24px;text-align:center;background:var(--surface);border-radius:44px 16px 44px 16px;color:var(--on-var)}
.empty b{display:block;font-size:20px;color:var(--on-surface);margin-bottom:4px}
footer{margin-top:36px;text-align:center;font-size:13px}
footer a{color:var(--on-var);text-decoration:underline}

/* ---------- inventory ---------- */
.tabs{margin:14px 0 0}
.search{display:flex;align-items:center;gap:12px;padding:0 22px;height:56px;background:var(--surface-hi);
  border-radius:999px;margin:18px 0 4px;transition:border-radius .4s var(--spring)}
.search:focus-within{border-radius:20px;outline:2px solid var(--p)}
.search input{flex:1;min-width:0;border:0;background:none;color:inherit;font:500 16px var(--font);outline:none}
.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:space-between;margin:14px 0 12px}
a.chip.tgl{padding:9px 16px;font-size:14px}
a.chip.tgl.on{background:var(--p);color:var(--on-p)}
.invs{display:grid;gap:8px}
.inv{background:var(--surface);border-radius:32px;overflow:hidden;transition:border-radius .45s var(--spring)}
.inv[open]{border-radius:36px 16px 36px 16px}
.inv.is-shiny{background:linear-gradient(100deg,var(--surface) 55%,var(--gold))}
.inv.gone{display:none}
.inv summary{list-style:none;display:flex;gap:14px;align-items:center;padding:12px 20px 12px 12px;cursor:pointer}
.inv summary::-webkit-details-marker{display:none}
.inv .meta{flex:1;min-width:0;display:grid;gap:6px}
.count{text-align:center;min-width:52px}
.count b{display:block;font-size:30px;font-weight:800;line-height:1;letter-spacing:-.02em}
.count small{font-size:12px;font-weight:600;color:var(--on-var)}
.rows{list-style:none;padding:4px 12px 14px;display:grid;gap:6px}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px 10px 18px;
  background:var(--bg);border-radius:22px}
.row[hidden]{display:none}
.row b{display:block;font-size:15px;overflow-wrap:anywhere}
.row.traded{opacity:.6}.row.traded b{text-decoration:line-through}
.tbtn{border:0;cursor:pointer;font:700 13px var(--font);padding:11px 16px;border-radius:999px;flex:none;
  background:var(--tc);color:var(--on-tc);transition:transform .35s var(--spring),border-radius .35s var(--spring)}
.tbtn:hover{border-radius:12px}.tbtn:active{transform:scale(.9)}
.tbtn:disabled{opacity:.5}
.row.traded .tbtn{background:var(--sc);color:var(--on-sc)}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));display:flex;align-items:center;gap:16px;
  max-width:calc(100% - 32px);padding:14px 14px 14px 22px;border-radius:22px;background:var(--on-surface);color:var(--bg);
  font-size:14px;font-weight:600;transform:translate(-50%,160%);transition:transform .5s var(--spring);z-index:9}
.toast.on{transform:translate(-50%,0)}
.toast button{border:0;cursor:pointer;font:700 14px var(--font);padding:9px 16px;border-radius:999px;background:var(--p);color:var(--on-p)}

@media(max-width:430px){.seg a{padding:9px 11px}.seg a.on{padding:9px 14px}}

/* ---------- icons (inline SVG, Material-3-expressive style: rounded, soft joins) ---------- */
.ic{width:1.15em;height:1.15em;flex:none;vertical-align:-.2em}
.icon-btn .ic{width:24px;height:24px}
.search .ic{width:22px;height:22px;color:var(--on-var)}
.mon small{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:3px}
.mon small .ic{width:13px;height:13px}
.chip .ic{width:1.1em;height:1.1em;vertical-align:0}
.ic-hundo{width:1.45em}
.sw[data-accent=ocean]{--c:#00629d}
.sw[data-accent=rose]{--c:#a8315a}
.sw[data-accent=forest]{--c:#1f6a3b}
.sw[data-accent=violet]{--c:#6445b6}
.sw[data-accent=sunset]{--c:#9a4600}
/* ---------- theme panel + footer links ---------- */
.theme{position:fixed;z-index:20;top:calc(env(safe-area-inset-top) + 84px);right:max(16px,calc((100vw - 860px)/2 + 16px));
  width:min(300px,calc(100vw - 32px));padding:18px;background:var(--surface);color:var(--on-surface);border-radius:32px 12px 32px 32px;
  box-shadow:0 0 0 1px var(--outline),0 18px 48px rgba(0,0,0,.28);transform-origin:top right;
  transform:scale(.85);opacity:0;visibility:hidden;transition:transform .4s var(--spring),opacity .2s,visibility 0s .4s}
.theme.on{transform:none;opacity:1;visibility:visible;transition:transform .4s var(--spring),opacity .2s}
.theme h3{font-size:14px;font-weight:700;margin:0 0 8px;color:var(--on-var)}
.theme h3~h3{margin-top:16px}
.theme .seg{display:flex}.theme .seg button{flex:1;justify-content:center}
.seg button{display:inline-flex;align-items:center;gap:6px;padding:9px 12px;border:0;border-radius:999px;font:600 14px var(--font);
  color:var(--on-var);background:none;cursor:pointer;transition:all .35s var(--spring)}
.seg button.on{background:var(--p);color:var(--on-p)}.seg button:active{transform:scale(.92)}
.sws{display:flex;gap:10px;flex-wrap:wrap}
.sw{width:44px;height:44px;border:0;border-radius:50%;background:var(--c);cursor:pointer;display:grid;place-items:center;color:#fff;
  transition:border-radius .35s var(--spring),transform .35s var(--spring)}
.sw:hover{border-radius:14px}.sw:active{transform:scale(.88)}
.sw .ic{width:22px;height:22px;opacity:0}.sw.on .ic{opacity:1}.sw.on{border-radius:14px}
.links{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.links a.chip{padding:10px 18px;font-size:14px;text-decoration:none;transition:transform .35s var(--spring),border-radius .35s var(--spring)}
.links a.chip:hover{border-radius:12px}.links a.chip:active{transform:scale(.92)}
.links a.chip.d{background:var(--pc);color:var(--on-pc)}.links a.chip.g{background:var(--tc);color:var(--on-tc)}
.sw:focus-visible,.seg button:focus-visible,.icon-btn:focus-visible,.links a:focus-visible{outline:2px solid var(--p);outline-offset:2px}

/* ---------- entrance motion ---------- */
@keyframes pop{from{opacity:0;transform:translateY(18px) scale(.94)}to{opacity:1;transform:none}}
.stat,.trainer,.mon,.catch,.inv{animation:pop .6s var(--spring) both;animation-delay:calc(min(var(--i,0),12) * 45ms)}
/* a refresh / live update of the same screen must not replay the intro animations */
.static .stat,.static .trainer,.static .mon,.static .catch,.static .inv{animation:none}
.static .fresh{animation:pop .6s var(--spring) both}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>

<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <symbol id="i-sparkle" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round">
    <path d="M10 6C10.64 10.4 13.6 13.36 18 14C13.6 14.64 10.64 17.6 10 22C9.36 17.6 6.4 14.64 2 14C6.4 13.36 9.36 10.4 10 6Z"/>
    <path d="M19 2C19.32 4.2 20.8 5.68 23 6C20.8 6.32 19.32 7.8 19 10C18.68 7.8 17.2 6.32 15 6C17.2 5.68 18.68 4.2 19 2Z"/>
  </symbol>
  <symbol id="i-hundo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">
    <rect x="1.5" y="4" width="21" height="16" rx="8" stroke-width="2"/>
    <path d="M6.3 10.2 7.8 9v6.2" stroke-width="1.7"/>
    <rect x="10.3" y="9" width="3" height="6.2" rx="1.5" stroke-width="1.7"/>
    <rect x="15.2" y="9" width="3" height="6.2" rx="1.5" stroke-width="1.7"/>
  </symbol>
  <symbol id="i-image" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3" y="4" width="18" height="16" rx="5"/>
    <circle cx="9" cy="10" r="1.5" fill="currentColor" stroke="none"/>
    <path d="M4.5 17.5 9.5 13.2 13 16.2 15.5 14 19.5 17.5"/>
  </symbol>
  <symbol id="i-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>
  </symbol>
  <symbol id="i-pin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 21.5C12 21.5 5 15.2 5 9.8a7 7 0 0 1 14 0c0 5.4-7 11.7-7 11.7z"/><circle cx="12" cy="9.8" r="2.4"/>
  </symbol>
  <symbol id="i-back" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M19 12H5M11 5.5 4.5 12 11 18.5"/>
  </symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M20 12A8 8 0 1 1 17.66 6.34"/><path d="M13.2 6.3H17.7V1.8"/>
  </symbol>
  <symbol id="i-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M5 12.5 9.5 17 19 7.5"/>
  </symbol>
  <symbol id="i-palette" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 3a9 9 0 1 0 0 18c1.4 0 2-1 1.6-2-.5-1.2.3-2.5 1.7-2.5H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z"/>
    <circle cx="7.5" cy="11" r="1.2" fill="currentColor" stroke="none"/><circle cx="10.5" cy="7.3" r="1.2" fill="currentColor" stroke="none"/><circle cx="15.2" cy="7.8" r="1.2" fill="currentColor" stroke="none"/>
  </symbol>
  <symbol id="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>
  </symbol>
  <symbol id="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z"/>
  </symbol>
  <symbol id="i-auto" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17" /><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/>
  </symbol>
  <symbol id="i-chat" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M5 4h14a2.5 2.5 0 0 1 2.5 2.5v8A2.5 2.5 0 0 1 19 17h-6.5L8 20.5V17H5a2.5 2.5 0 0 1-2.5-2.5v-8A2.5 2.5 0 0 1 5 4z"/><path d="M8 9.5h8M8 12.5h5"/>
  </symbol>
  <symbol id="i-code" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M8.5 7 3.5 12l5 5M15.5 7l5 5-5 5M13.5 5 10.5 19"/>
  </symbol>
</svg>
{% macro ic(n) %}<svg class="ic ic-{{n}}" aria-hidden="true"><use href="#i-{{n}}"/></svg>{% endmacro %}
<main>
{% set base = '?key=' ~ key %}
<header class="top">
  {% if sel %}<a class="icon-btn" href="{{base}}" aria-label="Back">{{ic('back')}}</a>{% endif %}
  <div class="grow">
    <p class="eyebrow">{{ 'Trainer' if sel else 'RegiBot Hub' }}</p>
    <h1>{{ sel or ('Inventory' if view == 'inventory' else 'Accounts') }}</h1>
  </div>
  <button class="icon-btn" data-theme-toggle aria-label="Theme" aria-haspopup="true" aria-expanded="false">{{ic('palette')}}</button>
  <button class="icon-btn spin" onclick="refresh(this)" aria-label="Refresh">{{ic('refresh')}}</button>
</header>
{% if not sel %}
<nav class="seg tabs">
  <a class="{{'on' if view != 'inventory'}}" href="{{base}}">Accounts</a>
  <a class="{{'on' if view == 'inventory'}}" href="{{base}}&view=inventory">Inventory</a>
</nav>
{% endif %}

<div id="view" data-idx="{{ 2 if sel else (1 if view == 'inventory' else 0) }}" data-traded="{{ 1 if show_traded else 0 }}">
{% if view == 'inventory' %}
<section class="stats">
  <div class="stat" style="--i:0"><b id="t-held">{{inv.held}}</b><span>Holding</span></div>
  <div class="stat" style="--i:1"><b id="t-species">{{inv.species}}</b><span>Species</span></div>
  <div class="stat" style="--i:2"><b class="num">{{ic('sparkle')}}<span id="t-shinies">{{inv.shinies}}</span></b><span>Shinies held</span></div>
  <div class="stat" style="--i:3"><b id="t-traded">{{inv.traded}}</b><span>Traded away</span></div>
</section>

<label class="search">{{ic('search')}}<input id="q" type="search" placeholder="Search Pokémon or account" autocomplete="off"></label>
<div class="tools">
  {% set iq = base ~ '&view=inventory' ~ ('&t=1' if show_traded else '') %}
  <nav class="seg">
    <a class="{{'on' if f=='all'}}" href="{{iq}}">All</a>
    <a class="{{'on' if f=='shiny'}}" href="{{iq}}&f=shiny">{{ic('sparkle')}}Shiny</a>
    <a class="{{'on' if f=='hundo'}}" href="{{iq}}&f=hundo">{{ic('hundo')}}Hundo</a>
    <a class="{{'on' if f=='bg'}}" href="{{iq}}&f=bg">{{ic('image')}}BG</a>
  </nav>
  <a class="chip tgl {{'on' if show_traded}}" href="{{base}}&view=inventory&f={{f}}{{'' if show_traded else '&t=1'}}">
    {% if show_traded %}{{ic('check')}}{% endif %}Show traded
  </a>
</div>

{% if groups %}
<div class="invs">
  {% for g in groups %}
  <details class="inv {{'is-shiny' if g.shiny}}" data-name="{{g.search}}" data-pokemon="{{g.pokemon}}" data-shiny="{{g.shiny}}" style="--i:{{loop.index0}}">
    <summary>
      <div class="thumb">{% if g.thumb %}<img loading="lazy" src="{{g.thumb}}" alt="">{% else %}{{g.pokemon[:1]}}{% endif %}</div>
      <div class="meta">
        <div class="c-title">{{g.pokemon}}{% if g.shiny %}<span class="chip shiny">{{ic('sparkle')}}Shiny</span>{% endif %}</div>
        <div class="c-chips acc">{% for a, n in g.accounts %}<span class="chip p">{{a}} ×{{n}}</span>{% endfor %}</div>
      </div>
      <div class="count"><b class="held">{{g.held}}</b><small>held</small></div>
    </summary>
    <ul class="rows">
      {% for i in g.mons %}
      <li class="row {{'traded' if i.traded}}" data-id="{{i.id}}" data-account="{{i.account}}" data-traded="{{i.traded}}">
        <div><b>{{i.account}}</b><span class="c-sub">IV {{i.iv or '-'}} · CP {{i.cp or '-'}} · {{i.ago}}</span>{% if i.background %} <span class="chip p">{{ic('image')}}BG</span>{% endif %}</div>
        <button class="tbtn" onclick="trade(this)">{{ 'Undo' if i.traded else 'Traded away' }}</button>
      </li>
      {% endfor %}
    </ul>
  </details>
  {% endfor %}
</div>
{% else %}
<div class="empty"><b>Nothing here</b>No Pokémon match. Catches show up as soon as RegiBot reports them.</div>
{% endif %}
<div class="toast" id="toast"></div>

{% else %}

<section class="stats">
  {% if sel %}
    <div class="stat" style="--i:0"><b>{{tot.caught}}</b><span>Caught</span></div>
    <div class="stat" style="--i:1"><b>{{tot.species}}</b><span>Species</span></div>
    <div class="stat" style="--i:2"><b class="num">{{ic('sparkle')}}{{tot.shinies}}</b><span>Shinies</span></div>
    <div class="stat" style="--i:3"><b>{{tot.hundos}}</b><span>Hundos</span></div>
  {% else %}
    <div class="stat" style="--i:0"><b>{{accounts|length}}</b><span>Accounts</span></div>
    <div class="stat" style="--i:1"><b>{{tot.caught}}</b><span>Caught</span></div>
    <div class="stat" style="--i:2"><b class="num">{{ic('sparkle')}}{{tot.shinies}}</b><span>Shinies</span></div>
    <div class="stat" style="--i:3"><b>{{tot.hundos}}</b><span>Hundos</span></div>
  {% endif %}
</section>

{% if sel %}
  <div class="sec"><h2>Collection</h2></div>
  {% if collection %}
  <div class="dex">
    {% for p in collection %}
    <div class="mon {{'is-shiny' if p.sh}}" data-key="{{p.pokemon}}" style="--i:{{loop.index0}}">
      {% if p.thumb %}<img loading="lazy" src="{{p.thumb}}" alt="">{% else %}<div class="ph">{{p.pokemon[:1]}}</div>{% endif %}
      <b>{{p.pokemon}}</b>
      <small>×{{p.n}}{% if p.sh %}<span>·</span>{{ic('sparkle')}}{{p.sh}}{% endif %}{% if p.best %} · {{p.best}}%{% endif %}</small>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty"><b>No catches yet</b>Catches from this trainer will show up here.</div>
  {% endif %}
{% endif %}

<div class="sec">
  <h2>Recent catches</h2>
  <nav class="seg">
    {% set q = base ~ ('&account=' ~ (sel|urlencode) if sel else '') %}
    <a class="{{'on' if f=='all'}}" href="{{q}}">All</a>
    <a class="{{'on' if f=='shiny'}}" href="{{q}}&f=shiny">{{ic('sparkle')}}Shiny</a>
    <a class="{{'on' if f=='hundo'}}" href="{{q}}&f=hundo">{{ic('hundo')}}Hundo</a>
    <a class="{{'on' if f=='bg'}}" href="{{q}}&f=bg">{{ic('image')}}BG</a>
  </nav>
</div>
{% if events %}
<ul class="catches">
  {% for e in events %}
  <li class="catch {{'is-shiny' if e.shiny}}" data-id="{{e.id}}" style="--i:{{loop.index0}}">
    <div class="thumb">{% if e.thumb %}<img loading="lazy" src="{{e.thumb}}" alt="">{% else %}{{e.pokemon[:1]}}{% endif %}</div>
    <div class="meta">
      <div class="c-title">{{e.pokemon}}{% if e.shiny %}<span class="chip shiny">{{ic('sparkle')}}Shiny</span>{% endif %}</div>
      <div class="c-sub">{% if not sel %}{{e.account}} · {% endif %}{{e.mode or 'Unknown mode'}} · {{e.ago}}</div>
      <div class="c-chips">
        {% if e.iv %}<span class="chip {{'hundo' if e.iv_pct==100}}">IV {{e.iv}}</span>{% endif %}
        {% if e.cp %}<span class="chip">CP {{e.cp}}</span>{% endif %}
        {% if e.background %}<span class="chip p">{{ic('image')}}Background</span>{% endif %}
        {% if e.lat %}<a class="chip p" href="https://maps.google.com/?q={{e.lat}},{{e.lon}}">{{ic('pin')}}Map</a>{% endif %}
      </div>
    </div>
  </li>
  {% endfor %}
</ul>
{% else %}
<div class="empty"><b>Nothing here</b>No catches match this filter.</div>
{% endif %}

{% if not sel %}
  <div class="sec"><h2>Trainers</h2></div>
  {% if accounts %}
  <div class="trainers">
    {% for a in accounts %}
    <a class="trainer" data-account="{{a.account}}" style="--i:{{loop.index0}}" href="{{base}}&account={{a.account|urlencode}}">
      <div class="avatar sh{{loop.index0 % 4}}">{{a.account[:1]|upper}}</div>
      <div><div class="t-name">{{a.account}}</div><div class="t-last">Active {{a.last}}</div></div>
      <div class="t-chips">
        <span class="chip p">{{a.caught}} caught</span>
        {% if a.shinies %}<span class="chip shiny">{{ic('sparkle')}}{{a.shinies}}</span>{% endif %}
        {% if a.hundos %}<span class="chip hundo">{{ic('hundo')}}{{a.hundos}}</span>{% endif %}
        {% if a.total %}<span class="chip">{{a.total}} total shinies</span>{% endif %}
      </div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty"><b>Nothing yet</b>Waiting for RegiBot to send its first webhook.</div>
  {% endif %}
{% endif %}


{% endif %}
</div>

<footer>
  <div class="links">
    <a class="chip d" href="{{discord_url}}" target="_blank" rel="noopener">{{ic('chat')}} Discord</a>
    <a class="chip g" href="{{source_url}}" target="_blank" rel="noopener">{{ic('code')}} GitHub &middot; source code</a>
  </div>
  {% if hidden %}<p style="margin-bottom:8px;color:var(--on-var)">{{hidden}} old / unrecognised account{{'s' if hidden != 1}} hidden</p>{% endif %}
  <a href="/raw?key={{key}}">Raw payloads</a>
</footer>
<div class="theme" id="theme" role="dialog" aria-label="Theme">
  <h3>Mode</h3>
  <div class="seg" id="modes">
    <button data-mode="auto">{{ic('auto')}} Auto</button>
    <button data-mode="light">{{ic('sun')}} Light</button>
    <button data-mode="dark">{{ic('moon')}} Dark</button>
  </div>
  <h3>Color</h3>
  <div class="sws" id="accents">
    <button class="sw" data-accent="ocean" aria-label="Ocean" title="Ocean">{{ic('check')}}</button>
    <button class="sw" data-accent="rose" aria-label="Rose" title="Rose">{{ic('check')}}</button>
    <button class="sw" data-accent="forest" aria-label="Forest" title="Forest">{{ic('check')}}</button>
    <button class="sw" data-accent="violet" aria-label="Violet" title="Violet">{{ic('check')}}</button>
    <button class="sw" data-accent="sunset" aria-label="Sunset" title="Sunset">{{ic('check')}}</button>
  </div>
</div>
</main>
<script>
const KEY = {{ key|tojson }};
const $ = id => document.getElementById(id);
const showTraded = () => $('view').dataset.traded === '1';
let tt;
function toast(msg, undo){
  const t = $('toast'); t.innerHTML = '';
  const s = document.createElement('span'); s.textContent = msg; t.appendChild(s);
  if (undo){ const b = document.createElement('button'); b.textContent = 'Undo';
    b.onclick = () => { t.classList.remove('on'); undo(); }; t.appendChild(b); }
  t.classList.add('on'); clearTimeout(tt); tt = setTimeout(() => t.classList.remove('on'), 6000);
}
function recount(card){
  const held = [...card.querySelectorAll('.row')].filter(r => r.dataset.traded !== '1');
  const by = {}; held.forEach(r => by[r.dataset.account] = (by[r.dataset.account] || 0) + 1);
  card.querySelector('.held').textContent = held.length;
  const acc = card.querySelector('.acc'); acc.innerHTML = '';
  Object.entries(by).sort((a, b) => b[1] - a[1]).forEach(([a, n]) => {
    const s = document.createElement('span'); s.className = 'chip p'; s.textContent = a + ' ×' + n; acc.appendChild(s); });
  card.classList.toggle('gone', !held.length && !showTraded());
}
async function flip(row, to){
  const btn = row.querySelector('.tbtn'), card = row.closest('.inv');
  btn.disabled = true;
  try {
    const r = await fetch('/trade/' + row.dataset.id + '?key=' + encodeURIComponent(KEY) + '&v=' + to, {method: 'POST'});
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    ['held', 'species', 'shinies', 'traded'].forEach(k => $('t-' + k).textContent = d[k]);
  } catch (e) { btn.disabled = false; toast('Could not save, try again'); return false; }
  row.dataset.traded = to; row.classList.toggle('traded', !!to);
  btn.textContent = to ? 'Undo' : 'Traded away'; btn.disabled = false;
  row.hidden = !!to && !showTraded();
  if (!to) card.classList.remove('gone');
  recount(card);
  return true;
}
async function trade(btn){
  const row = btn.closest('.row'), card = btn.closest('.inv');
  const to = row.dataset.traded === '1' ? 0 : 1;
  if (await flip(row, to) && to){
    toast(card.dataset.pokemon + ' (' + row.dataset.account + ') marked traded', () => flip(row, 0));
  }
}
document.addEventListener('input', e => {
  if (e.target.id !== 'q') return;
  const v = e.target.value.toLowerCase().trim();
  document.querySelectorAll('.inv').forEach(c => c.hidden = !c.dataset.name.includes(v));
});

/* ---- in-page navigation: swap content instead of reloading ---- */
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
const SLIDE = 56;
let navId = 0;
async function go(url){
  const id = ++navId;
  let doc;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    if (!doc.getElementById('view')) throw new Error('bad page');
  } catch (e) { location.href = url; return; }
  if (id !== navId) return;

  const ov = $('view'), nv = doc.getElementById('view');
  const dir = Math.sign(Number(nv.dataset.idx) - Number(ov.dataset.idx));
  const animate = dir && !reduceMotion.matches;

  if (animate) {
    const out = ov.animate(
      [{opacity: 1, transform: 'none'}, {opacity: 0, transform: 'translateX(' + (-dir * SLIDE) + 'px)'}],
      {duration: 150, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards'});
    try { await out.finished; } catch (e) {}
    if (id !== navId) { out.cancel(); return; }
  }

  /* keep search text + opened cards when staying on the same screen */
  const keep = {q: '', open: new Set()};
  if (!dir) {
    keep.q = $('q') ? $('q').value : '';
    document.querySelectorAll('.inv[open]').forEach(d => keep.open.add(d.dataset.pokemon + '|' + d.dataset.shiny));
  }

  const oh = document.querySelector('.top'), nh = doc.querySelector('.top');
  if (oh.outerHTML !== nh.outerHTML) oh.replaceWith(nh);
  const ot = document.querySelector('nav.tabs'), nt = doc.querySelector('nav.tabs');
  if (ot && nt) [...ot.children].forEach((a, i) => a.className = nt.children[i].className);
  else if (ot) ot.remove();
  else if (nt) ov.before(nt);
  const of = document.querySelector('footer'), nf = doc.querySelector('footer');
  if (of.outerHTML !== nf.outerHTML) of.replaceWith(nf);
  if (!dir) nv.classList.add('static');  /* same screen: no intro animations again */
  ov.replaceWith(nv);

  nv.querySelectorAll('.inv').forEach(d => { if (keep.open.has(d.dataset.pokemon + '|' + d.dataset.shiny)) d.open = true; });
  if (keep.q && $('q')) { $('q').value = keep.q; $('q').dispatchEvent(new Event('input', {bubbles: true})); }
  if (dir) window.scrollTo(0, 0);

  if (animate) nv.animate(
    [{opacity: 0, transform: 'translateX(' + (dir * SLIDE) + 'px)'}, {opacity: 1, transform: 'none'}],
    {duration: 450, easing: 'cubic-bezier(.34,1.3,.64,1)'});
}
function refresh(btn){
  if (!reduceMotion.matches) btn.animate([{transform: 'rotate(0)'}, {transform: 'rotate(360deg)'}],
    {duration: 600, easing: 'cubic-bezier(.34,1.2,.64,1)'});
  go(location.href);
}
document.addEventListener('click', e => {
  if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest('a[href]');
  if (!a || a.target || a.origin !== location.origin || a.pathname !== location.pathname) return;
  e.preventDefault();
  if (a.href === location.href) return;
  history.pushState(null, '', a.href);
  go(a.href);
});
addEventListener('popstate', () => go(location.href));

/* ---- live updates: merge new catches in place, no reload ---- */
function merge(ol, nl, key, light){
  const fresh = new Map([...nl.children].map(n => [n.dataset[key], n]));
  const have = new Map();
  [...ol.children].forEach(o => { if (fresh.has(o.dataset[key])) have.set(o.dataset[key], o); else o.remove(); });
  let ref = ol.firstElementChild;
  fresh.forEach((n, k) => {
    let el = have.get(k);
    if (el) {
      if (light) {
        const a = el.querySelector('.c-sub'), b = n.querySelector('.c-sub');
        if (a && b && a.textContent !== b.textContent) a.textContent = b.textContent;
      } else if (el.innerHTML !== n.innerHTML) el.innerHTML = n.innerHTML;
      if (el.className !== n.className) el.className = n.className;
    } else { el = n; el.style.setProperty('--i', 0); el.classList.add('fresh'); }
    if (el === ref) ref = ref.nextElementSibling; else ol.insertBefore(el, ref);
  });
}
async function live(){
  const v = $('view');
  if (document.hidden || v.dataset.idx === '1') return;
  const id = navId;
  let doc;
  try {
    const r = await fetch(location.href, {cache: 'no-store'});
    if (!r.ok) return;
    doc = new DOMParser().parseFromString(await r.text(), 'text/html');
  } catch (e) { return; }
  const nv = doc.getElementById('view');
  if (id !== navId || !nv || v !== $('view') || nv.dataset.idx !== v.dataset.idx) return;
  const os = v.querySelectorAll('.stat'), ns = nv.querySelectorAll('.stat');
  ns.forEach((n, i) => { if (os[i] && os[i].innerHTML !== n.innerHTML) os[i].innerHTML = n.innerHTML; });
  let rebuild = false;
  [['.trainers', 'account', false], ['.dex', 'key', false], ['.catches', 'id', true]].forEach(([sel, key, light]) => {
    const o = v.querySelector(sel), n = nv.querySelector(sel);
    if (!!o !== !!n) rebuild = true; else if (o) merge(o, n, key, light);
  });
  if (rebuild) go(location.href);
}

/* ---- theme: mode (auto/light/dark) + accent colour, saved in this browser ---- */
const root = document.documentElement;
function store(k, v){ try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} }
function applyTheme(){
  const mode = root.dataset.mode || 'auto', acc = root.dataset.accent || 'ocean';
  document.querySelectorAll('#modes button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  document.querySelectorAll('#accents .sw').forEach(b => b.classList.toggle('on', b.dataset.accent === acc));
  const m = document.querySelector('meta[name=theme-color]');
  if (m) m.content = getComputedStyle(root).getPropertyValue('--bg').trim();
}
function openTheme(on){
  $('theme').classList.toggle('on', on);
  document.querySelectorAll('[data-theme-toggle]').forEach(b => b.setAttribute('aria-expanded', on));
}
document.addEventListener('click', e => {
  const tg = e.target.closest('[data-theme-toggle]'), box = $('theme');
  if (tg) { openTheme(!box.classList.contains('on')); return; }
  const mb = e.target.closest('#modes button'), ab = e.target.closest('#accents .sw');
  if (mb) { mb.dataset.mode === 'auto' ? delete root.dataset.mode : root.dataset.mode = mb.dataset.mode;
            store('hub-mode', mb.dataset.mode === 'auto' ? null : mb.dataset.mode); applyTheme(); return; }
  if (ab) { ab.dataset.accent === 'ocean' ? delete root.dataset.accent : root.dataset.accent = ab.dataset.accent;
            store('hub-accent', ab.dataset.accent === 'ocean' ? null : ab.dataset.accent); applyTheme(); return; }
  if (!e.target.closest('#theme')) openTheme(false);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') openTheme(false); });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
applyTheme();
setInterval(live, 8000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) live(); });
</script>
"""


def ago(ts):
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def inventory(f):
    show_traded = request.args.get("t") == "1"
    with db() as c:
        rows = c.execute("""SELECT id, ts, account, pokemon, shiny, iv, iv_pct, cp, thumb,
                            COALESCE(traded,0) traded, COALESCE(background,0) background FROM events
                            WHERE event='caught' AND ok(account) ORDER BY ts DESC""").fetchall()
        inv = inv_stats(c)
        hidden = hidden_count(c)
    groups = {}
    for r in rows:
        if r["traded"] and not show_traded:
            continue
        if f == "shiny" and not r["shiny"]:
            continue
        if f == "hundo" and r["iv_pct"] != 100:
            continue
        if f == "bg" and not r["background"]:
            continue
        g = groups.setdefault((r["pokemon"], r["shiny"]),
                              dict(pokemon=r["pokemon"], shiny=r["shiny"], thumb=None, mons=[]))
        g["thumb"] = g["thumb"] or r["thumb"]
        g["mons"].append(dict(r, ago=ago(r["ts"])))
    glist = sorted(groups.values(), key=lambda g: (g["pokemon"].lower(), -g["shiny"]))
    for g in glist:
        cnt = Counter(i["account"] for i in g["mons"] if not i["traded"])
        g["accounts"] = cnt.most_common()
        g["held"] = sum(cnt.values())
        g["search"] = (g["pokemon"] + (" shiny " if g["shiny"] else " ") +
                       " ".join(i["account"] for i in g["mons"]) +
                       (" background" if any(i["background"] for i in g["mons"]) else "")).lower()
    return render_template_string(PAGE, view="inventory", groups=glist, inv=inv, f=f,
                                  show_traded=show_traded, key=SECRET, sel=None, hidden=hidden,
                                  accounts=[], events=[], collection=[], tot={})


@app.get("/")
def dash():
    if request.args.get("key") != SECRET:
        abort(404)
    sel = request.args.get("account")
    f = request.args.get("f", "all")
    if f not in ("all", "shiny", "hundo", "bg"):
        f = "all"
    view = request.args.get("view", "")
    if view == "inventory" and not sel:
        return inventory(f)
    with db() as c:
        accts = [dict(r, last=ago(r["m"])) for r in c.execute("""
            SELECT account,
              SUM(event='caught') caught,
              SUM(event='caught' AND shiny=1) shinies,
              SUM(event='caught' AND iv_pct=100) hundos,
              MAX(total_shinies) total, MAX(ts) m
            FROM events WHERE ok(account) AND event!='encountered' GROUP BY account ORDER BY m DESC""")]
        hidden = hidden_count(c)
        collection = []
        if sel:
            collection = c.execute("""
                SELECT pokemon, COUNT(*) n, SUM(shiny) sh, MAX(iv_pct) best,
                  (SELECT thumb FROM events e2 WHERE e2.account=e.account
                     AND e2.pokemon=e.pokemon AND e2.event='caught' AND e2.thumb IS NOT NULL
                   ORDER BY e2.shiny DESC, e2.ts DESC LIMIT 1) thumb
                FROM events e WHERE account=? AND event='caught' AND COALESCE(traded,0)=0
                GROUP BY pokemon ORDER BY n DESC""", (sel,)).fetchall()
        where, args = ["event='caught'", "ok(account)"], []
        if sel:
            where.append("account=?"); args.append(sel)
        if f == "shiny":
            where.append("shiny=1")
        if f == "hundo":
            where.append("iv_pct=100")
        if f == "bg":
            where.append("background=1")
        rows = c.execute(f"SELECT * FROM events WHERE {' AND '.join(where)} "
                         "ORDER BY ts DESC LIMIT 100", args).fetchall()
    events = [dict(r, ago=ago(r["ts"])) for r in rows]

    scope = [a for a in accts if a["account"] == sel] if sel else accts
    tot = dict(caught=sum(a["caught"] or 0 for a in scope),
               shinies=sum(a["shinies"] or 0 for a in scope),
               hundos=sum(a["hundos"] or 0 for a in scope),
               species=len(collection))
    return render_template_string(PAGE, view=view, accounts=accts, events=events, sel=sel, f=f,
                                  collection=collection, tot=tot, key=SECRET, hidden=hidden)


def hidden_count(c):
    return c.execute("SELECT COUNT(DISTINCT account) FROM events WHERE NOT ok(account) AND event!='encountered'").fetchone()[0]


def inv_stats(c):
    rows = c.execute("SELECT pokemon, shiny, COALESCE(traded,0) t FROM events "
                     "WHERE event='caught' AND ok(account)").fetchall()
    held = [r for r in rows if not r["t"]]
    return dict(held=len(held),
                species=len({(r["pokemon"], r["shiny"]) for r in held}),
                shinies=sum(1 for r in held if r["shiny"]),
                traded=len(rows) - len(held))


@app.post("/trade/<int:eid>")
def trade(eid):
    if request.args.get("key") != SECRET:
        abort(404)
    v = 1 if request.args.get("v") == "1" else 0
    with db() as c:
        cur = c.execute("UPDATE events SET traded=?, traded_ts=? WHERE id=? AND event='caught'",
                        (v, time.time() if v else None, eid))
        if cur.rowcount == 0:
            abort(404)
        return jsonify(ok=True, state=v, **inv_stats(c))


@app.get("/raw")
def raw():
    if request.args.get("key") != SECRET:
        abort(404)
    with db() as c:
        rows = c.execute("SELECT raw FROM events ORDER BY ts DESC LIMIT 20").fetchall()
    return jsonify([json.loads(r["raw"]) for r in rows])


if __name__ == "__main__":
    if DISCORD_TOKEN and CHANNEL_ID:
        threading.Thread(target=poll_discord, daemon=True).start()
    else:
        print("DISCORD_TOKEN / CHANNEL_ID not set - webhook endpoint only")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
