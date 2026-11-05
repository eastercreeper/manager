import os, re, sqlite3, time, urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template
from apscheduler.schedulers.background import BackgroundScheduler

DB_PATH = os.getenv("DB_PATH", "/data/anime-manager.sqlite3")
NYAA_BASE = os.getenv("NYAA_BASE", "https://nyaa.si/")
QBT_URL = os.getenv("QBITTORRENT_URL", "http://127.0.0.1:30024").rstrip("/")
QBT_USER = os.getenv("QBITTORRENT_USERNAME", "")
QBT_PASS = os.getenv("QBITTORRENT_PASSWORD", "")
QBT_CATEGORY = os.getenv("QBITTORRENT_CATEGORY", "anime")
QBT_SAVE_PATH = os.getenv("QBITTORRENT_SAVE_PATH", "/downloads/anime")
MIN_QUALITY = os.getenv("MIN_QUALITY", "1080p").lower()
SCAN_INTERVAL_MINUTES = max(5, int(os.getenv("SCAN_INTERVAL_MINUTES", "30")))

scheduler = BackgroundScheduler(daemon=True)
app = FastAPI(title="Anime Manager", version="0.1.0")

PAGE = Template(r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Anime Manager</title>
<style>
body{font:15px system-ui;background:#11131a;color:#e8eaf0;margin:0;padding:24px;max-width:1100px;margin:auto}
h1,h2{margin:.3em 0 .7em} .muted{color:#a4a9b8} .panel{background:#1b1f2a;border:1px solid #303646;border-radius:12px;padding:16px;margin:14px 0}
input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #42495b;background:#10131b;color:#fff}
button{background:#6654d9;border:0;cursor:pointer} .danger{background:#85464b} form.inline{display:inline}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px 7px;border-bottom:1px solid #303646;vertical-align:top}
a{color:#b9adff}.pill{font-size:12px;color:#c7cbe0}.row{display:flex;gap:8px;flex-wrap:wrap}
</style></head><body>
<h1>Anime Manager</h1><p class="muted">Manual tracking · Nyaa RSS discovery · approval before qBittorrent</p>
<div class="panel"><h2>Add anime</h2>
<form method="post" action="/shows">
<input name="title" placeholder="Anime title" required>
<input name="search_query" placeholder="Optional search override">
<button>Add anime</button></form>
<p class="muted">Search override is useful for alternate titles or fansub naming. Scanning only queues candidates; it never downloads automatically.</p></div>
<div class="panel"><div class="row"><h2 style="flex:1">Tracked anime</h2><form method="post" action="/scan"><button>Scan sources now</button></form></div>
<table><tr><th>Anime</th><th>Search query</th><th>Action</th></tr>
{% for s in shows %}<tr><td>{{s["title"]}}</td><td>{{s["search_query"]}}</td><td><form class="inline" method="post" action="/shows/{{s['id']}}/delete"><button class="danger">Remove</button></form></td></tr>{% else %}<tr><td colspan="3" class="muted">No anime added yet.</td></tr>{% endfor %}</table></div>
<div class="panel"><h2>Approval queue</h2>
<table><tr><th>Release</th><th>Source</th><th>Anime</th><th>Quality</th><th>Action</th></tr>
{% for r in releases %}<tr><td><a href="{{r['page_url']}}" target="_blank" rel="noreferrer">{{r['title']}}</a><br><span class="pill">{{r['published']}}</span></td><td>{{r['source']}}</td><td>{{r['show_title']}}</td><td>{{r['quality']}}</td><td>
<form class="inline" method="post" action="/releases/{{r['id']}}/approve"><button>Approve & add</button></form>
<form class="inline" method="post" action="/releases/{{r['id']}}/reject"><button class="danger">Reject</button></form>
</td></tr>{% else %}<tr><td colspan="5" class="muted">No pending releases. Add anime and scan.</td></tr>{% endfor %}</table></div>
<p class="muted">v0.1 · TsukiHime adapter is intentionally disabled until its published API schema is verified.</p>
</body></html>
""")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS shows(
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, search_query TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS releases(
        id INTEGER PRIMARY KEY, show_id INTEGER NOT NULL, source TEXT NOT NULL,
        source_id TEXT NOT NULL, title TEXT NOT NULL, page_url TEXT NOT NULL,
        torrent_url TEXT, info_hash TEXT, published TEXT, quality TEXT,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
        UNIQUE(source, source_id))""")
    conn.commit()
    return conn

def extract_hash(entry):
    # Nyaa's RSS extension exposes an info hash in common feed parsers.
    for key in ("nyaa_infohash", "infohash", "nyaa:infoHash"):
        val = entry.get(key)
        if val:
            return str(val).strip()
    for link in entry.get("links", []):
        href = link.get("href", "")
        if href.startswith("magnet:"):
            m = re.search(r"btih:([a-fA-F0-9]{40})", href)
            if m: return m.group(1).lower()
    return None

def extract_torrent_url(entry):
    for link in entry.get("links", []):
        href = link.get("href", "")
        typ = (link.get("type") or "").lower()
        if href.startswith("magnet:") or "bittorrent" in typ or href.endswith(".torrent"):
            return href
    enclosure = entry.get("enclosures", [])
    for item in enclosure:
        href = item.get("href", "")
        if href: return href
    return None

def scan_nyaa(conn):
    shows = conn.execute("SELECT * FROM shows").fetchall()
    added = 0
    for show in shows:
        query = show["search_query"] + " 1080p"
        url = NYAA_BASE + "?" + urllib.parse.urlencode({"page":"rss","q":query,"c":"1_2","f":"0"})
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title or "1080p" not in title.lower():
                continue
            info_hash = extract_hash(entry)
            torrent_url = extract_torrent_url(entry)
            page_url = entry.get("link", NYAA_BASE)
            source_id = info_hash or entry.get("id") or page_url
            if not torrent_url and info_hash:
                torrent_url = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title)}"
            quality = "1080p"
            published = entry.get("published", "")
            try:
                cur = conn.execute("""INSERT OR IGNORE INTO releases
                    (show_id,source,source_id,title,page_url,torrent_url,info_hash,published,quality,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,'pending',?)""",
                    (show["id"],"Nyaa",str(source_id),title,page_url,torrent_url,info_hash,published,quality,
                     datetime.now(timezone.utc).isoformat()))
                added += cur.rowcount
            except sqlite3.Error:
                pass
    conn.commit()
    return added

def scheduled_scan():
    try:
        with closing(db()) as conn:
            scan_nyaa(conn)
    except Exception as exc:
        # Keep the scheduler alive; details are visible in container logs.
        print(f"Scheduled scan failed: {exc}", flush=True)

@app.on_event("startup")
def start_scheduler():
    scheduler.add_job(scheduled_scan, "interval", minutes=SCAN_INTERVAL_MINUTES,
                      id="nyaa-scan", replace_existing=True, max_instances=1)
    scheduler.start()

@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)

def qbt_add(url):
    if not QBT_USER or not QBT_PASS:
        raise RuntimeError("Set QBITTORRENT_USERNAME and QBITTORRENT_PASSWORD before approving releases.")
    if not url:
        raise RuntimeError("This release has no magnet or torrent download URL.")
    sess = requests.Session()
    login = sess.post(f"{QBT_URL}/api/v2/auth/login",
                      data={"username":QBT_USER,"password":QBT_PASS},
                      headers={"Referer":QBT_URL}, timeout=15)
    if login.status_code != 200 or login.text.strip() != "Ok.":
        raise RuntimeError(f"qBittorrent login failed (HTTP {login.status_code}).")
    resp = sess.post(f"{QBT_URL}/api/v2/torrents/add",
                     data={"urls":url,"savepath":QBT_SAVE_PATH,
                           "category":QBT_CATEGORY,"paused":"false"},
                     headers={"Referer":QBT_URL}, timeout=30)
    if resp.status_code != 200 or resp.text.strip() != "Ok.":
        raise RuntimeError(f"qBittorrent rejected the torrent (HTTP {resp.status_code}).")

@app.get("/", response_class=HTMLResponse)
def home():
    with closing(db()) as conn:
        shows = conn.execute("SELECT * FROM shows ORDER BY title").fetchall()
        releases = conn.execute("""SELECT r.*,s.title AS show_title FROM releases r
            JOIN shows s ON s.id=r.show_id WHERE r.status='pending'
            ORDER BY r.id DESC LIMIT 150""").fetchall()
        return PAGE.render(shows=shows, releases=releases)

@app.post("/shows")
def add_show(title: str = Form(...), search_query: str = Form("")):
    title = title.strip()
    if not title: raise HTTPException(400, "Title required")
    query = search_query.strip() or title
    with closing(db()) as conn:
        conn.execute("INSERT INTO shows(title,search_query,created_at) VALUES(?,?,?)",
                     (title,query,datetime.now(timezone.utc).isoformat()))
        conn.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/shows/{show_id}/delete")
def delete_show(show_id: int):
    with closing(db()) as conn:
        conn.execute("DELETE FROM releases WHERE show_id=?", (show_id,))
        conn.execute("DELETE FROM shows WHERE id=?", (show_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/scan")
def scan():
    with closing(db()) as conn:
        added = scan_nyaa(conn)
    return RedirectResponse("/", status_code=303)

@app.post("/releases/{release_id}/approve")
def approve(release_id: int):
    with closing(db()) as conn:
        release = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not release: raise HTTPException(404, "Release not found")
        if release["status"] != "pending": return RedirectResponse("/", status_code=303)
        try:
            qbt_add(release["torrent_url"])
        except Exception as exc:
            raise HTTPException(502, str(exc))
        conn.execute("UPDATE releases SET status='approved' WHERE id=?", (release_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/releases/{release_id}/reject")
def reject(release_id: int):
    with closing(db()) as conn:
        conn.execute("UPDATE releases SET status='rejected' WHERE id=?", (release_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)

@app.get("/health")
def health():
    return {"status":"ok","version":"0.1.0"}
