import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import feedparser
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template

DB_PATH = os.getenv("DB_PATH", "/data/anime-manager.sqlite3")
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/ivan/Shows"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
NYAA_BASE = os.getenv("NYAA_BASE", "https://nyaa.si/").rstrip("/") + "/"
TSUKIHIME_BASE = os.getenv("TSUKIHIME_BASE", "https://api.tsukihime.org/v1").rstrip("/")
QBT_URL = os.getenv("QBITTORRENT_URL", "http://127.0.0.1:30024").rstrip("/")
QBT_CATEGORY = os.getenv("QBITTORRENT_CATEGORY", "anime")
SCAN_INTERVAL_MINUTES = max(5, int(os.getenv("SCAN_INTERVAL_MINUTES", "30")))
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
JELLYFIN_PATH_PREFIXES = [p for p in os.getenv(
    "JELLYFIN_PATH_PREFIXES",
    "/mnt/Jellyfin/JellyfinData/JP-BEAN/Shows=/ivan/Shows",
).split(",") if "=" in p]
DB_LOCK = threading.Lock()
scheduler = BackgroundScheduler(daemon=True)
app = FastAPI(title="Anime Manager", version="1.0.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
    INSERT INTO schema_version(version) SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
    CREATE TABLE IF NOT EXISTS anime (
      id INTEGER PRIMARY KEY, title TEXT NOT NULL, folder_name TEXT NOT NULL,
      search_query TEXT NOT NULL, jellyfin_id TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'tracking',
      year INTEGER, poster_url TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS alternate_titles (
      id INTEGER PRIMARY KEY, anime_id INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
      title TEXT NOT NULL, language TEXT
    );
    CREATE TABLE IF NOT EXISTS provider_ids (
      id INTEGER PRIMARY KEY, anime_id INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
      provider TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(anime_id, provider, value)
    );
    CREATE TABLE IF NOT EXISTS episodes (
      id INTEGER PRIMARY KEY, anime_id INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
      season INTEGER, episode INTEGER, title TEXT, air_date TEXT, status TEXT NOT NULL DEFAULT 'expected',
      jellyfin_id TEXT, jellyfin_path TEXT, filesystem_path TEXT, has_file BOOLEAN NOT NULL DEFAULT 0,
      UNIQUE(anime_id, season, episode)
    );
    CREATE TABLE IF NOT EXISTS releases (
      id INTEGER PRIMARY KEY, anime_id INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
      source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL, page_url TEXT NOT NULL,
      torrent_url TEXT, info_hash TEXT, published TEXT, group_name TEXT, quality TEXT,
      episode INTEGER, audio_hint TEXT, subtitle_hint TEXT, codec TEXT, size_bytes INTEGER,
      seeders INTEGER, leechers INTEGER, score REAL NOT NULL DEFAULT 0, score_reason TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, UNIQUE(source, source_id)
    );
    CREATE TABLE IF NOT EXISTS downloads (
      id INTEGER PRIMARY KEY, release_id INTEGER UNIQUE NOT NULL REFERENCES releases(id),
      torrent_hash TEXT, name TEXT, progress REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued',
      save_path TEXT, error TEXT, approved_at TEXT, completed_at TEXT, imported_at TEXT
    );
    CREATE TABLE IF NOT EXISTS media_verifications (
      id INTEGER PRIMARY KEY, download_id INTEGER NOT NULL REFERENCES downloads(id),
      file_path TEXT NOT NULL, audio_json TEXT NOT NULL, subtitle_json TEXT NOT NULL,
      english_ass BOOLEAN NOT NULL DEFAULT 0, japanese_ass BOOLEAN NOT NULL DEFAULT 0,
      accepted BOOLEAN NOT NULL DEFAULT 0, error TEXT, verified_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS imported_files (
      id INTEGER PRIMARY KEY, anime_id INTEGER NOT NULL REFERENCES anime(id), episode_id INTEGER,
      source_path TEXT NOT NULL, destination_path TEXT UNIQUE NOT NULL, imported_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS scan_history (
      id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
      status TEXT NOT NULL, message TEXT
    );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)")}
    for name, definition in (
        ("jellyfin_path", "TEXT"),
        ("filesystem_path", "TEXT"),
        ("has_file", "BOOLEAN NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE episodes ADD COLUMN {name} {definition}")
    conn.commit()
    return conn


def setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def configured_path(key: str, fallback: Path) -> Path:
    with closing(db()) as conn:
        return Path(setting(conn, key, str(fallback)))


def safe_folder(name: str) -> str:
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("Invalid series folder name")
    value = re.sub(r"[\x00-\x1f/\\]+", " ", name).strip(" .")
    if not value or value in {".", ".."} or ".." in value.split():
        raise ValueError("Invalid series folder name")
    return value[:180]


def map_jellyfin_path(path: str) -> Path:
    mappings = list(JELLYFIN_PATH_PREFIXES)
    if not any(path == mapping.split("=", 1)[0] or path.startswith(mapping.split("=", 1)[0].rstrip("/") + "/")
               for mapping in mappings):
        mappings.append("/ivan/Shows=/ivan/Shows")
    for mapping in sorted(mappings, key=lambda value: len(value.split("=", 1)[0]), reverse=True):
        source, target = mapping.split("=", 1) if isinstance(mapping, str) else mapping
        if path == source or path.startswith(source.rstrip("/") + "/"):
            return Path(target) / path[len(source):].lstrip("/")
    return Path(path)


VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".webm", ".m4v", ".ts"}


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def scan_series_files(series_path: Path) -> dict[tuple[int, int], Path]:
    files: dict[tuple[int, int], Path] = {}
    if not series_path.is_dir():
        return files
    pattern = re.compile(r"(?:s(\d{1,2})\s*e(\d{1,3})|\b(\d{1,3})\b)", re.IGNORECASE)
    for path in series_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        match = pattern.search(path.stem)
        if not match:
            continue
        season = int(match.group(1) or 1)
        episode = int(match.group(2) or match.group(3))
        files.setdefault((season, episode), path)
    return files


class JellyfinClient:
    def __init__(self, url: str = JELLYFIN_URL, api_key: str = JELLYFIN_API_KEY):
        if not url or not api_key:
            with closing(db()) as conn:
                url = url or setting(conn, "jellyfin_url")
                api_key = api_key or setting(conn, "jellyfin_api_key")
        self.url, self.api_key = url.rstrip("/"), api_key
        if self.url and urllib.parse.urlparse(self.url).scheme not in {"http", "https"}:
            raise ValueError("Jellyfin URL must use http or https")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.url or not self.api_key:
            raise RuntimeError("Jellyfin is not configured")
        headers = kwargs.pop("headers", {})
        headers["X-Emby-Token"] = self.api_key
        headers.setdefault("Accept", "application/json")
        response = requests.request(method, self.url + path, headers=headers, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def libraries(self) -> list[dict]:
        payload = self.request("GET", "/Library/VirtualFolders")
        return payload if isinstance(payload, list) else payload.get("Items", [])

    def shows_library(self) -> Optional[dict]:
        candidates = []
        for library in self.libraries():
            collection_type = (library.get("CollectionType") or "").lower()
            name = (library.get("Name") or "").lower()
            if collection_type in {"tvshows", "tv shows"}:
                return library
            if name in {"shows", "anime"}:
                candidates.append(library)
        return candidates[0] if candidates else None

    def series(self) -> list[dict]:
        library = self.shows_library()
        if not library:
            return []
        parent_id = library.get("ItemId") or library.get("Id")
        params = {"IncludeItemTypes": "Series",
                  "Recursive": "true",
                  "Fields": "ProviderIds,Path,PremiereDate,Overview,Genres,OriginalTitle,SortName"}
        if parent_id:
            params["ParentId"] = parent_id
        payload = self.request("GET", "/Items", params=params)
        return payload.get("Items", []) if isinstance(payload, dict) else []

    def episodes(self, series_id: str) -> list[dict]:
        payload = self.request("GET", "/Shows/" + series_id + "/Episodes",
                               params={"Fields": "Path,ProviderIds,PremiereDate"})
        return payload.get("Items", []) if isinstance(payload, dict) else []

    def refresh(self) -> None:
        self.request("POST", "/Library/Refresh")


class QBitClient:
    def __init__(self):
        with closing(db()) as conn:
            self.url = os.getenv("QBITTORRENT_URL", setting(conn, "qbittorrent_url", QBT_URL)).rstrip("/")
            self.user = os.getenv("QBITTORRENT_USERNAME", setting(conn, "qbittorrent_username"))
            self.password = os.getenv("QBITTORRENT_PASSWORD", setting(conn, "qbittorrent_password"))

    def session(self) -> requests.Session:
        if not self.user or not self.password:
            raise RuntimeError("qBittorrent credentials are not configured")
        sess = requests.Session()
        response = sess.post(self.url + "/api/v2/auth/login",
                             data={"username": self.user, "password": self.password},
                             headers={"Referer": self.url}, timeout=15)
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent login failed")
        return sess

    def add(self, torrent_url: str, save_path: str = str(DOWNLOAD_DIR)) -> None:
        sess = self.session()
        response = sess.post(self.url + "/api/v2/torrents/add",
                             data={"urls": torrent_url, "savepath": save_path,
                                   "category": QBT_CATEGORY, "paused": "false"},
                             headers={"Referer": self.url}, timeout=30)
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent rejected the torrent")

    def torrents(self) -> list[dict]:
        return self.session().get(self.url + "/api/v2/torrents/info",
                                  params={"category": QBT_CATEGORY}, timeout=20).json()


def inspect_media(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,codec_name,codec_long_name:stream_tags=language,title",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=60, check=True)
    streams = json.loads(result.stdout).get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    acceptable = {"ass", "ssa", "ass/ssa"}
    english = any((s.get("tags", {}).get("language", "").lower() in {"en", "eng", "english"} and
                   s.get("codec_name", "").lower() in acceptable) for s in subtitles)
    japanese = any((s.get("tags", {}).get("language", "").lower() in {"ja", "jpn", "japanese"} and
                    s.get("codec_name", "").lower() in acceptable) for s in subtitles)
    return {"audio": audio, "subtitles": subtitles, "english_ass": english, "japanese_ass": japanese}


def score_release(release: dict) -> tuple[float, str]:
    title = release.get("title", "").lower()
    points, reasons = 0.0, []
    if "multi" in title or "dual audio" in title:
        points += 40; reasons.append("multi-audio hint")
    if re.search(r"\b(jpn|japanese)\b", title):
        points += 25; reasons.append("Japanese audio hint")
    if re.search(r"\beng(?:lish)?\b", title):
        points += 20; reasons.append("English subtitle hint")
    if "1080" in title:
        points += 15; reasons.append("1080p")
    seeders = int(release.get("seeders") or 0)
    points += min(10, seeders / 10)
    if seeders:
        reasons.append(f"{seeders} seeders")
    return points, ", ".join(reasons) or "limited metadata; inspect media before import"


def extract_hash(entry: dict) -> Optional[str]:
    for key in ("nyaa_infohash", "infohash", "nyaa:infoHash"):
        if entry.get(key):
            return str(entry[key]).strip().lower()
    for link in entry.get("links", []):
        match = re.search(r"btih:([a-fA-F0-9]{40})", link.get("href", ""))
        if match:
            return match.group(1).lower()
    return None


def extract_torrent_url(entry: dict) -> Optional[str]:
    for link in entry.get("links", []) + entry.get("enclosures", []):
        href, typ = link.get("href", ""), (link.get("type") or "").lower()
        if href.startswith("magnet:") or "bittorrent" in typ or href.endswith(".torrent"):
            return href
    return None


def parse_nyaa(show: sqlite3.Row) -> list[dict]:
    query = show["search_query"]
    url = NYAA_BASE + "?" + urllib.parse.urlencode({"page": "rss", "q": query, "c": "1_2", "f": "0"})
    feed = feedparser.parse(requests.get(url, timeout=20).content)
    releases = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        info_hash = extract_hash(entry)
        torrent_url = extract_torrent_url(entry)
        if not title:
            continue
        if not torrent_url and info_hash:
            torrent_url = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title)}"
        if not torrent_url:
            continue
        release = {"source": "Nyaa", "source_id": info_hash or entry.get("id") or entry.get("link"),
                   "title": title, "page_url": entry.get("link", NYAA_BASE), "torrent_url": torrent_url,
                   "info_hash": info_hash, "published": entry.get("published", "")}
        release["score"], release["score_reason"] = score_release(release)
        releases.append(release)
    return releases


def parse_tsukihime(show: sqlite3.Row) -> list[dict]:
    try:
        response = requests.get(TSUKIHIME_BASE + "/openapi.json", timeout=10)
        if response.status_code != 200:
            return []
        spec = response.json()
        endpoint = next((p for p in spec.get("paths", {}) if "search" in p.lower() or "release" in p.lower()), None)
        if not endpoint:
            return []
        response = requests.get(TSUKIHIME_BASE + endpoint, params={"q": show["search_query"]}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("data", payload.get("results", []))
        return [{"source": "TsukiHime", "source_id": str(x.get("id") or x.get("url")),
                 "title": x.get("title", ""), "page_url": x.get("url", TSUKIHIME_BASE),
                 "torrent_url": x.get("magnet") or x.get("torrent_url"), "info_hash": x.get("info_hash"),
                 "published": x.get("published", ""), **score_release(x)[0:1]} for x in items if x.get("title")]
    except (requests.RequestException, ValueError, StopIteration, KeyError):
        return []


def scan_sources() -> int:
    with DB_LOCK, closing(db()) as conn:
        total = 0
        for show in conn.execute("SELECT * FROM anime").fetchall():
            for release in parse_nyaa(show) + parse_tsukihime(show):
                conn.execute("""INSERT OR IGNORE INTO releases
                  (anime_id,source,source_id,title,page_url,torrent_url,info_hash,published,score,score_reason,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                             (show["id"], release["source"], str(release["source_id"]), release["title"],
                              release["page_url"], release.get("torrent_url"), release.get("info_hash"),
                              release.get("published"), release.get("score", 0), release.get("score_reason", ""), now()))
                total += conn.execute("SELECT changes()").fetchone()[0]
        conn.execute("INSERT INTO scan_history(kind,started_at,finished_at,status,message) VALUES(?,?,?,?,?)",
                     ("releases", now(), now(), "ok", f"{total} new releases"))
        conn.commit()
        return total


def sync_jellyfin() -> int:
    client = JellyfinClient()
    count = 0
    for item in client.series():
        title = item.get("Name", "Unknown")
        reported_path = item.get("Path", "")
        mapped_series_path = map_jellyfin_path(reported_path) if reported_path else Path("")
        folder = safe_folder(mapped_series_path.name or title)
        provider_ids = item.get("ProviderIds") or {}
        with DB_LOCK, closing(db()) as conn:
            normalized = normalize_title(title)
            row = conn.execute("SELECT id FROM anime WHERE jellyfin_id=?", (item.get("Id"),)).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT id FROM anime WHERE lower(title)=lower(?) OR lower(folder_name)=lower(?) "
                    "ORDER BY jellyfin_id IS NULL DESC LIMIT 1", (title, folder)
                ).fetchone()
            if row:
                anime_id = row["id"]
                conn.execute("""UPDATE anime SET title=?, folder_name=?, search_query=?, jellyfin_id=?,
                    year=?, metadata_json=?, updated_at=? WHERE id=?""",
                             (title, folder, title, item.get("Id"),
                              (item.get("PremiereDate") or "")[:4] or None,
                              json.dumps(item), now(), anime_id))
            else:
                cur = conn.execute("""INSERT INTO anime(title,folder_name,search_query,jellyfin_id,year,metadata_json,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                                   (title, folder, title, item.get("Id"), (item.get("PremiereDate") or "")[:4] or None,
                                    json.dumps(item), now(), now()))
                anime_id = cur.lastrowid
            for provider, value in provider_ids.items():
                conn.execute("INSERT OR IGNORE INTO provider_ids(anime_id,provider,value) VALUES(?,?,?)",
                             (anime_id, provider, value))
            alternate_titles = list(item.get("AlternateTitles") or [])
            alternate_titles.extend(
                value for value in (item.get("OriginalTitle"), item.get("SortName"))
                if value
            )
            if isinstance(alternate_titles, list):
                for alternate in alternate_titles:
                    alternate_value = alternate if isinstance(alternate, str) else alternate.get("Title")
                    if alternate_value and normalize_title(alternate_value) != normalized:
                        conn.execute("INSERT OR IGNORE INTO alternate_titles(anime_id,title,language) VALUES(?,?,?)",
                                     (anime_id, alternate_value, alternate.get("Language") if isinstance(alternate, dict) else None))
            physical_files = scan_series_files(mapped_series_path) if mapped_series_path.is_absolute() else {}
            for episode in client.episodes(item["Id"]):
                season = episode.get("ParentIndexNumber")
                number = episode.get("IndexNumber")
                if number is not None:
                    physical = physical_files.get((season or 1, number))
                    conn.execute("""INSERT INTO episodes(
                        anime_id,season,episode,title,status,jellyfin_id,jellyfin_path,filesystem_path,has_file
                    ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(anime_id,season,episode) DO UPDATE SET
                        title=excluded.title,jellyfin_id=excluded.jellyfin_id,jellyfin_path=excluded.jellyfin_path,
                        filesystem_path=excluded.filesystem_path,has_file=excluded.has_file,
                        status=CASE WHEN excluded.has_file OR excluded.jellyfin_id IS NOT NULL THEN 'available' ELSE episodes.status END""",
                                 (anime_id, season or 1, number, episode.get("Name"),
                                  "available", episode.get("Id"), episode.get("Path"),
                                  str(physical) if physical else None, bool(physical or episode.get("Path"))))
            for (season, number), physical in physical_files.items():
                conn.execute("""INSERT OR IGNORE INTO episodes(
                    anime_id,season,episode,status,filesystem_path,has_file
                ) VALUES(?,?,?,?,?,1)""",
                             (anime_id, season, number, "available", str(physical)))
            conn.commit()
        count += 1
    return count


def import_completed() -> int:
    imported = 0
    with DB_LOCK, closing(db()) as conn:
        for download in conn.execute("SELECT d.*,r.anime_id,a.folder_name,a.title FROM downloads d JOIN releases r ON r.id=d.release_id JOIN anime a ON a.id=r.anime_id WHERE d.state='completed' AND d.imported_at IS NULL").fetchall():
            root = Path(download["save_path"] or configured_path("download_dir", DOWNLOAD_DIR))
            files = [p for p in root.rglob("*") if p.suffix.lower() in {".mkv", ".mp4", ".avi", ".webm"}]
            for source in files:
                try:
                    media = inspect_media(source)
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    conn.execute("INSERT INTO media_verifications(download_id,file_path,audio_json,subtitle_json,error,verified_at) VALUES(?,?,?,?,?,?)",
                                 (download["id"], str(source), "[]", "[]", str(exc), now()))
                    continue
                accepted = bool(media["english_ass"])
                verification = conn.execute("""INSERT INTO media_verifications(download_id,file_path,audio_json,subtitle_json,english_ass,japanese_ass,accepted,verified_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (download["id"], str(source), json.dumps(media["audio"]), json.dumps(media["subtitles"]),
                     media["english_ass"], media["japanese_ass"], accepted, now()))
                if not accepted:
                    conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                                 ("English ASS/SSA subtitle not found", download["id"]))
                    continue
                destination_dir = configured_path("library_dir", LIBRARY_DIR) / safe_folder(download["folder_name"]) / "Season 01"
                destination_dir.mkdir(parents=True, exist_ok=True)
                destination = destination_dir / f"{safe_folder(download['title'])} - {source.name}"
                if destination.exists():
                    continue
                shutil.copy2(source, destination)
                conn.execute("INSERT INTO imported_files(anime_id,source_path,destination_path,imported_at) VALUES(?,?,?,?)",
                             (download["anime_id"], str(source), str(destination), now()))
                conn.execute("UPDATE downloads SET imported_at=?,state='imported' WHERE id=?",
                             (now(), download["id"]))
                imported += 1
                break
        conn.commit()
    if imported and JELLYFIN_URL and JELLYFIN_API_KEY:
        try:
            JellyfinClient().refresh()
        except requests.RequestException:
            pass
    return imported


def background_sync() -> None:
    try:
        sync_jellyfin()
    except (RuntimeError, requests.RequestException, sqlite3.Error) as exc:
        print(f"Jellyfin sync failed: {exc}", flush=True)
    try:
        scan_sources()
    except (RuntimeError, requests.RequestException, sqlite3.Error) as exc:
        print(f"Release scan failed: {exc}", flush=True)
    try:
        import_completed()
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"Import failed: {exc}", flush=True)


def monitor_downloads() -> None:
    """Persist qBittorrent progress; importing remains gated by media verification."""
    try:
        torrents = {str(t.get("hash", "")).lower(): t for t in QBitClient().torrents()}
    except (RuntimeError, requests.RequestException):
        return
    with DB_LOCK, closing(db()) as conn:
        rows = conn.execute("SELECT id,torrent_hash FROM downloads WHERE imported_at IS NULL").fetchall()
        for row in rows:
            torrent = torrents.get((row["torrent_hash"] or "").lower())
            if not torrent:
                continue
            state = "completed" if float(torrent.get("progress", 0)) >= 1 else str(torrent.get("state", "downloading"))
            conn.execute("UPDATE downloads SET progress=?,state=?,name=?,save_path=?,completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,?) ELSE completed_at END WHERE id=?",
                         (float(torrent.get("progress", 0)), state, torrent.get("name"), torrent.get("save_path"),
                          state, now(), row["id"]))
        conn.commit()


PAGE = Template(r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Anime Manager</title><style>
body{font:15px system-ui;background:#10121a;color:#e8eaf0;margin:0 auto;padding:24px;max-width:1250px}h1,h2{margin:.3em 0 .7em}
.muted{color:#a4a9b8}.panel{background:#1b1f2a;border:1px solid #303646;border-radius:12px;padding:16px;margin:14px 0}
input,button,select{font:inherit;padding:9px;border-radius:8px;border:1px solid #42495b;background:#10131b;color:#fff}button{background:#6654d9;border:0;cursor:pointer}
button.danger{background:#85464b}form.inline{display:inline}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px 7px;border-bottom:1px solid #303646;vertical-align:top}a{color:#b9adff}.pill{font-size:12px;color:#c7cbe0}
nav a{margin-right:15px} .stat{font-size:24px;font-weight:700}
</style></head><body><h1>Anime Manager</h1><nav><a href="/">Dashboard</a><a href="/library">My Library</a><a href="/episodes">New Episodes</a><a href="/downloads">Downloads</a><a href="/history">History</a><a href="/settings">Settings</a></nav>
{% if message %}<p class="panel">{{message}}</p>{% endif %}{{ body|safe }}</body></html>""")


def page(body: str, message: str = "") -> HTMLResponse:
    return HTMLResponse(PAGE.render(body=body, message=message))


@app.on_event("startup")
def start_scheduler() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db().close()
    scheduler.add_job(background_sync, "interval", minutes=SCAN_INTERVAL_MINUTES, id="sync", replace_existing=True, max_instances=1)
    scheduler.add_job(monitor_downloads, "interval", minutes=2, id="downloads", replace_existing=True, max_instances=1)
    scheduler.start()


@app.on_event("shutdown")
def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    with closing(db()) as conn:
        stats = {key: conn.execute(query).fetchone()[0] for key, query in {
            "anime": "SELECT count(*) FROM anime", "airing": "SELECT count(*) FROM anime WHERE status='airing'",
            "pending": "SELECT count(*) FROM releases WHERE status='pending'",
            "attention": "SELECT count(*) FROM downloads WHERE state='needs_review'"}.items()}
        releases = conn.execute("""SELECT r.*,a.title anime_title FROM releases r JOIN anime a ON a.id=r.anime_id
            WHERE r.status='pending' ORDER BY r.score DESC,r.id DESC LIMIT 50""").fetchall()
    body = Template("""<div class="row">{% for k,v in stats.items() %}<div class="panel"><div class="muted">{{k}}</div><div class="stat">{{v}}</div></div>{% endfor %}</div>
    <div class="panel"><div class="row"><h2 style="flex:1">Awaiting approval</h2><form method="post" action="/sync"><button>Sync now</button></form></div>
    <table><tr><th>Release</th><th>Anime</th><th>Score</th><th>Action</th></tr>{% for r in releases %}<tr><td><a href="{{r.page_url}}" target="_blank" rel="noreferrer">{{r.title}}</a><br><span class="muted">{{r.source}} · {{r.score_reason}}</span></td><td>{{r.anime_title}}</td><td>{{"%.1f"|format(r.score)}}</td><td><form class="inline" method="post" action="/releases/{{r.id}}/approve"><button>Approve</button></form><form class="inline" method="post" action="/releases/{{r.id}}/reject"><button class="danger">Reject</button></form></td></tr>{% else %}<tr><td colspan="4" class="muted">Nothing awaiting approval.</td></tr>{% endfor %}</table></div>""").render(stats=stats, releases=releases)
    return page(body)


@app.get("/library", response_class=HTMLResponse)
def library() -> HTMLResponse:
    with closing(db()) as conn:
        shows = conn.execute("SELECT a.*,count(e.id) episodes FROM anime a LEFT JOIN episodes e ON e.anime_id=a.id GROUP BY a.id ORDER BY a.title").fetchall()
    body = Template("""<div class="panel"><div class="row"><h2 style="flex:1">Tracked anime</h2><form method="post" action="/library/sync"><button>Synchronize Jellyfin library</button></form></div><p class="muted">Jellyfin is the metadata source of truth; filesystem scanning verifies physical episode files.</p><form method="post" action="/anime"><input name="title" placeholder="Anime title" required><input name="folder_name" placeholder="Custom folder name"><button>Add / Track</button></form></div>
    <div class="panel"><table><tr><th>Anime</th><th>Folder</th><th>Episodes</th><th>Status</th></tr>{% for s in shows %}<tr><td>{{s.title}}</td><td>{{s.folder_name}}</td><td>{{s.episodes}}</td><td>{{s.status}}</td></tr>{% else %}<tr><td colspan="4">No anime tracked.</td></tr>{% endfor %}</table></div>""").render(shows=shows)
    return page(body)


@app.post("/anime")
def add_anime(title: str = Form(...), folder_name: str = Form("")) -> RedirectResponse:
    title, folder_name = title.strip(), safe_folder(folder_name.strip() or title)
    with closing(db()) as conn:
        conn.execute("INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (title, folder_name, title, now(), now()))
        conn.commit()
    return RedirectResponse("/library", status_code=303)


@app.post("/library/sync")
def library_sync() -> RedirectResponse:
    try:
        discovered = sync_jellyfin()
    except (RuntimeError, ValueError, requests.RequestException) as exc:
        raise HTTPException(502, str(exc)) from exc
    return RedirectResponse(f"/library?sync={discovered}", status_code=303)


@app.post("/sync")
def sync() -> RedirectResponse:
    background_sync()
    return RedirectResponse("/", status_code=303)


@app.get("/episodes", response_class=HTMLResponse)
def episodes() -> HTMLResponse:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT e.*,a.title AS anime_title FROM episodes e JOIN anime a ON a.id=e.anime_id
            ORDER BY e.air_date DESC LIMIT 100""").fetchall()
    body = Template("""<div class="panel"><h2>Episodes</h2><table><tr><th>Anime</th><th>Episode</th><th>Title</th><th>Status</th></tr>{% for e in rows %}<tr><td>{{e.anime_title}}</td><td>S{{"%02d"|format(e.season or 1)}}E{{"%02d"|format(e.episode)}}</td><td>{{e.title}}</td><td>{{e.status}}</td></tr>{% else %}<tr><td colspan="4">No episode metadata yet. Run Sync now.</td></tr>{% endfor %}</table></div>""").render(rows=rows)
    return page(body)


@app.get("/downloads", response_class=HTMLResponse)
def downloads() -> HTMLResponse:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT d.*,r.title FROM downloads d JOIN releases r ON r.id=d.release_id ORDER BY d.id DESC""").fetchall()
    body = Template("""<div class="panel"><h2>Downloads</h2><table><tr><th>Release</th><th>State</th><th>Progress</th><th>Verification</th></tr>{% for d in rows %}<tr><td>{{d.title}}</td><td>{{d.state}}</td><td>{{"%.1f"|format(d.progress*100)}}%</td><td>{{d.error or "pending"}}</td></tr>{% else %}<tr><td colspan="4">No approved downloads.</td></tr>{% endfor %}</table></div>""").render(rows=rows)
    return page(body)


@app.get("/history", response_class=HTMLResponse)
def history() -> HTMLResponse:
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM scan_history ORDER BY id DESC LIMIT 50").fetchall()
    body = Template("""<div class="panel"><h2>History</h2><table><tr><th>Kind</th><th>Status</th><th>Message</th><th>Finished</th></tr>{% for r in rows %}<tr><td>{{r.kind}}</td><td>{{r.status}}</td><td>{{r.message}}</td><td>{{r.finished_at}}</td></tr>{% endfor %}</table></div>""").render(rows=rows)
    return page(body)


@app.get("/settings", response_class=HTMLResponse)
def settings() -> HTMLResponse:
    with closing(db()) as conn:
        values = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM settings")}
    body = Template("""<div class="panel"><h2>Settings</h2><form method="post"><p>Jellyfin URL <input name="jellyfin_url" value="{{values.get('jellyfin_url','')}}" size="45"></p><p>Jellyfin API key <input name="jellyfin_api_key" type="password" placeholder="unchanged if blank" size="45"></p><p>qBittorrent URL <input name="qbittorrent_url" value="{{values.get('qbittorrent_url','')}}" size="45"></p><p>qBittorrent username <input name="qbittorrent_username" value="{{values.get('qbittorrent_username','')}}" size="25"></p><p>qBittorrent password <input name="qbittorrent_password" type="password" placeholder="unchanged if blank" size="25"></p><p>Library path <input name="library_dir" value="{{values.get('library_dir','/ivan/Shows')}}" size="35"></p><p>Download path <input name="download_dir" value="{{values.get('download_dir','/downloads')}}" size="35"></p><button>Save settings</button></form><p class="muted">Secrets are never returned in frontend responses. Environment variables take precedence.</p></div>""").render(values=values)
    return page(body)


@app.post("/settings")
def save_settings(jellyfin_url: str = Form(""), jellyfin_api_key: str = Form(""),
                  qbittorrent_url: str = Form(""), qbittorrent_username: str = Form(""),
                  qbittorrent_password: str = Form(""), library_dir: str = Form("/ivan/Shows"),
                  download_dir: str = Form("/downloads")) -> RedirectResponse:
    if jellyfin_url and urllib.parse.urlparse(jellyfin_url).scheme not in {"http", "https"}:
        raise HTTPException(400, "Jellyfin URL must use http or https")
    with closing(db()) as conn:
        values = {"jellyfin_url": jellyfin_url.rstrip("/"), "qbittorrent_url": qbittorrent_url.rstrip("/"),
                  "qbittorrent_username": qbittorrent_username, "library_dir": library_dir, "download_dir": download_dir}
        if jellyfin_api_key:
            values["jellyfin_api_key"] = jellyfin_api_key
        if qbittorrent_password:
            values["qbittorrent_password"] = qbittorrent_password
        for key, value in values.items():
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/releases/{release_id}/approve")
def approve(release_id: int) -> RedirectResponse:
    with DB_LOCK, closing(db()) as conn:
        release = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not release:
            raise HTTPException(404, "Release not found")
        if release["status"] != "pending":
            return RedirectResponse("/", status_code=303)
        if not release["torrent_url"]:
            raise HTTPException(400, "Release has no torrent or magnet URL")
        try:
            before = {str(t.get("hash", "")).lower() for t in QBitClient().torrents()}
            save_path = configured_path("download_dir", DOWNLOAD_DIR)
            QBitClient().add(release["torrent_url"], str(save_path))
            after = [t for t in QBitClient().torrents() if str(t.get("hash", "")).lower() not in before]
        except (RuntimeError, requests.RequestException) as exc:
            raise HTTPException(502, str(exc)) from exc
        conn.execute("UPDATE releases SET status='approved' WHERE id=?", (release_id,))
        save_path = configured_path("download_dir", DOWNLOAD_DIR)
        conn.execute("INSERT INTO downloads(release_id,torrent_hash,state,save_path,approved_at) VALUES(?,?,?,?,?)",
                     (release_id, (after[0].get("hash") if after else None), "queued", str(save_path), now()))
        conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/releases/{release_id}/reject")
def reject(release_id: int) -> RedirectResponse:
    with closing(db()) as conn:
        conn.execute("UPDATE releases SET status='rejected' WHERE id=? AND status='pending'", (release_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health() -> dict:
    with closing(db()) as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "version": app.version, "scheduler": scheduler.running}
