import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
import hashlib
import hmac
import secrets
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import feedparser
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, select_autoescape

DB_PATH = os.getenv("DB_PATH", "/data/anime-manager.sqlite3")
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/ivan/Shows"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
NYAA_BASE = os.getenv("NYAA_BASE", "https://nyaa.si/").rstrip("/") + "/"
TSUKIHIME_BASE = os.getenv("TSUKIHIME_BASE", "https://api.tsukihime.org/v1").rstrip("/")
QBT_URL = os.getenv("QBITTORRENT_URL", "http://127.0.0.1:30024").rstrip("/")
QBT_CATEGORY = os.getenv("QBITTORRENT_CATEGORY", "anime")
QBT_SAVE_PATH = Path(os.getenv("QBITTORRENT_SAVE_PATH", "/downloads/anime"))
SCAN_INTERVAL_MINUTES = max(5, int(os.getenv("SCAN_INTERVAL_MINUTES", "30")))
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
JELLYFIN_PATH_PREFIXES = [p for p in os.getenv(
    "JELLYFIN_PATH_PREFIXES",
    "/mnt/Jellyfin/JellyfinData/JP-BEAN/Shows=/ivan/Shows",
).split(",") if "=" in p]
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "").lower() in {"1", "true", "yes"}
SESSION_COOKIE = "anime_manager_session"
CSRF_COOKIE = "anime_manager_csrf"
DB_LOCK = threading.Lock()
scheduler = BackgroundScheduler(daemon=True)
app = FastAPI(title="Anime Manager", version="1.0.0")
templates = Environment(autoescape=select_autoescape(["html", "xml"]))
_process_secret = secrets.token_bytes(32)


def template(source: str):
    return templates.from_string(source)


def signing_secret() -> bytes:
    return (SESSION_SECRET or ADMIN_PASSWORD).encode() or _process_secret


def session_cookie() -> str:
    value = str(int(datetime.now(timezone.utc).timestamp()))
    signature = hmac.new(signing_secret(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{signature}"


def valid_session(value: str) -> bool:
    try:
        timestamp, signature = value.split(".", 1)
        if abs(int(datetime.now(timezone.utc).timestamp()) - int(timestamp)) > 86400:
            return False
        expected = hmac.new(signing_secret(), timestamp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def csrf_token(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE, "")


def require_csrf(request: Optional[Request], token: str = "") -> None:
    if request is not None and (not token or not hmac.compare_digest(token, csrf_token(request))):
        raise HTTPException(403, "Invalid CSRF token")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if ADMIN_PASSWORD:
        if request.url.path not in {"/login", "/health"} and not valid_session(request.cookies.get(SESSION_COOKIE, "")):
            if request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return HTMLResponse("Authentication required", status_code=401)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/health":
            expected = request.cookies.get(CSRF_COOKIE, "")
            body = await request.body()
            encoded = urllib.parse.urlencode({"csrf_token": expected}).encode()
            if not expected or encoded not in body:
                return HTMLResponse("Invalid CSRF token", status_code=403)
            request._body = body
    response = await call_next(request)
    if ADMIN_PASSWORD and not request.cookies.get(CSRF_COOKIE) and CSRF_COOKIE not in response.headers.get("set-cookie", ""):
        response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), httponly=False, samesite="strict", secure=SECURE_COOKIES)
    return response


def path_is_within(root: Path, candidate: Path) -> bool:
    try:
        root = root.resolve()
        candidate = candidate.resolve()
        return candidate == root or root in candidate.parents
    except OSError:
        return False


def ensure_runtime_paths() -> None:
    for path in (DATA_DIR, QBT_SAVE_PATH):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".anime-manager-write-test-{secrets.token_hex(8)}"
            probe.touch()
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Anime Manager cannot write to {path}; grant the configured APP_UID/APP_GID write ACL access. "
                f"Refusing to run with elevated privileges: {exc}"
            ) from exc
    if not LIBRARY_DIR.is_dir():
        raise RuntimeError(
            f"Anime Manager cannot write to {LIBRARY_DIR}; ensure the /ivan/Shows mount exists and "
            "grant the configured APP_UID/APP_GID write ACL access. Refusing to run with elevated privileges."
        )
    try:
        probe = LIBRARY_DIR / f".anime-manager-write-test-{secrets.token_hex(8)}"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Anime Manager cannot write to {LIBRARY_DIR}; grant the configured APP_UID/APP_GID write ACL access. "
            f"Refusing to run with elevated privileges: {exc}"
        ) from exc


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
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, folder_locked BOOLEAN NOT NULL DEFAULT 0
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
      save_path TEXT, error TEXT, approved_at TEXT, completed_at TEXT, imported_at TEXT,
      submission_key TEXT, reconcile_attempts INTEGER NOT NULL DEFAULT 0, last_checked TEXT,
      content_path TEXT
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
    download_columns = {row["name"] for row in conn.execute("PRAGMA table_info(downloads)")}
    for name, definition in (
        ("submission_key", "TEXT"),
        ("reconcile_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_checked", "TEXT"),
        ("content_path", "TEXT"),
    ):
        if name not in download_columns:
            conn.execute(f"ALTER TABLE downloads ADD COLUMN {name} {definition}")
    anime_columns = {row["name"] for row in conn.execute("PRAGMA table_info(anime)")}
    if "folder_locked" not in anime_columns:
        conn.execute("ALTER TABLE anime ADD COLUMN folder_locked BOOLEAN NOT NULL DEFAULT 0")
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
    for path in series_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if not path_is_within(series_path, path):
            continue
        season, episodes = parse_file_episodes(path)
        if not episodes:
            continue
        for episode in episodes:
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
        headers["Authorization"] = f'MediaBrowser Token="{self.api_key}"'
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

    def search(self, query: str) -> list[dict]:
        payload = self.request("GET", "/Items", params={
            "SearchTerm": query, "IncludeItemTypes": "Series", "Recursive": "true",
            "Fields": "ProviderIds,Path,PremiereDate,Overview,Genres,OriginalTitle,SortName",
        })
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

    def add(self, torrent_url: str, save_path: Optional[str] = None) -> None:
        scheme = urllib.parse.urlparse(torrent_url).scheme.lower()
        if scheme not in {"magnet", "http", "https"}:
            raise RuntimeError("Unsupported torrent URL")
        sess = self.session()
        response = sess.post(self.url + "/api/v2/torrents/add",
                             data={"urls": torrent_url, "savepath": save_path or str(QBT_SAVE_PATH),
                                   "category": QBT_CATEGORY, "paused": "false"},
                             headers={"Referer": self.url}, timeout=30)
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent rejected the torrent")

    def torrents(self) -> list[dict]:
        response = self.session().get(self.url + "/api/v2/torrents/info",
                                      params={"category": QBT_CATEGORY}, timeout=20)
        response.raise_for_status()
        return response.json()

    def torrent_properties(self, torrent_hash: str) -> dict:
        response = self.session().get(self.url + "/api/v2/torrents/properties",
                                      params={"hash": torrent_hash}, timeout=20)
        response.raise_for_status()
        return response.json()

    def find(self, info_hash: Optional[str] = None, name: str = "") -> Optional[dict]:
        normalized_hash = (info_hash or "").lower()
        normalized_name = name.casefold().strip()
        for torrent in self.torrents():
            if normalized_hash and str(torrent.get("hash", "")).lower() == normalized_hash:
                return torrent
            if normalized_name and str(torrent.get("name", "")).casefold() == normalized_name:
                return torrent
        return None


def inspect_media(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,codec_name,codec_long_name:stream_tags=language,title",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=60, check=True)
    streams = json.loads(result.stdout).get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    acceptable = {"ass", "ssa", "ass/ssa"}

    def language(stream: dict) -> str:
        tags = stream.get("tags", {})
        values = [str(tags.get(key, "")) for key in ("language", "title")]
        tokens = {token for value in values for token in re.split(r"[^a-z]+", value.lower()) if token}
        if tokens & {"en", "eng", "english"}:
            return "eng"
        if tokens & {"ja", "jpn", "japanese"}:
            return "jpn"
        return ""

    english = any(language(s) == "eng" and s.get("codec_name", "").lower() in acceptable for s in subtitles)
    japanese = any(language(s) == "jpn" and s.get("codec_name", "").lower() in acceptable for s in subtitles)
    external = []
    for candidate in path.parent.glob(path.stem + ".*"):
        if candidate.suffix.lower() not in {".ass", ".ssa", ".srt"}:
            continue
        language_tokens = {token for token in re.split(r"[^a-z]+", candidate.stem.lower()) if token}
        external.append({"path": str(candidate), "codec_name": candidate.suffix[1:].lower(),
                         "tags": {"language": "jpn" if language_tokens & {"jpn", "ja", "japanese"}
                                  else "eng" if language_tokens & {"eng", "en", "english"} else ""}})
    subtitles.extend(external)
    english = english or any(
        s["tags"]["language"] == "eng" and s["codec_name"] in acceptable
        for s in external
    )
    japanese = japanese or any(
        s["tags"]["language"] == "jpn" and s["codec_name"] in acceptable
        for s in external
    )
    audio_languages = [language(stream) for stream in audio]
    return {
        "audio": audio, "subtitles": subtitles, "english_ass": english, "japanese_ass": japanese,
        "audio_languages": audio_languages, "japanese_audio": "jpn" in audio_languages,
        "multiple_audio": len(audio) > 1,
    }


def parse_file_episodes(path: Path) -> tuple[int, list[int]]:
    stem = path.stem
    season_match = re.search(r"(?:^|[^a-z])s(\d{1,2})(?=e|[^a-z]|$)", stem, re.IGNORECASE)
    season = int(season_match.group(1)) if season_match else 1
    cross_match = re.search(r"\b(\d{1,2})x(\d{1,3})(?:[-&](?:\d{1,2}x)?(\d{1,3}))?\b", stem, re.IGNORECASE)
    if cross_match:
        start = int(cross_match.group(2))
        end = int(cross_match.group(3) or start)
        return int(cross_match.group(1)), list(range(start, end + 1))
    range_match = re.search(r"\b(?:s\d{1,2}\s*)?e(\d{1,3})(?:[-&]e?(\d{1,3})|e(\d{1,3}))?\b", stem, re.IGNORECASE)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2) or range_match.group(3) or start)
        return season, list(range(start, end + 1))
    explicit = re.search(r"\b(?:ep?|episode)[ ._-]*(\d{1,3})(?:[-&](\d{1,3}))?\b", stem, re.IGNORECASE)
    if explicit:
        start = int(explicit.group(1))
        end = int(explicit.group(2) or start)
        return season, list(range(start, end + 1))
    candidates = []
    for match in re.finditer(r"(?<![A-Za-z])(\d{1,3})(?![A-Za-z])", stem):
        value = int(match.group(1))
        after = stem[match.end():].lower()
        if value in {360, 480, 576, 720, 1080, 1440, 2160, 264, 265, 266} or value >= 1900:
            continue
        if re.match(r"\s*(?:p|bit|fps|x\d+)", after):
            continue
        candidates.append(value)
    if len(candidates) == 1:
        return season, candidates
    return season, []


def safe_filename(value: str) -> str:
    value = re.sub(r"[\x00-\x1f/\\]+", " ", value).strip(" .")
    value = re.sub(r"[^A-Za-z0-9._ -]", "_", value)
    if not value or value in {".", ".."}:
        raise ValueError("Invalid media filename")
    return value[:240]


def safe_external_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value or "")
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else "#"


def library_destination(root: Path, folder_name: str, season: int, filename: str) -> Path:
    root = root.resolve()
    destination_dir = (root / safe_folder(folder_name) / f"Season {season:02d}").resolve()
    if root != destination_dir and root not in destination_dir.parents:
        raise ValueError("Library destination escapes configured library directory")
    destination_dir.mkdir(parents=True, exist_ok=True)
    return destination_dir / safe_filename(filename)


def release_title_matches(show: sqlite3.Row, title: str, aliases: Optional[list[str]] = None) -> bool:
    """Only accept releases containing the complete tracked title or a known alias."""
    candidates = [show["title"], show["folder_name"], show["search_query"], *(aliases or [])]
    normalized_release = normalize_title(title)
    return any(
        len(normalize_title(str(candidate or ""))) >= 5
        and normalize_title(str(candidate or "")) in normalized_release
        for candidate in candidates
    )


def parse_release_episodes(title: str) -> tuple[Optional[int], list[int]]:
    """Extract explicit episode numbers/ranges; do not guess from resolution or year."""
    season = 1
    sm = re.search(r"(?i)(?:^|[^a-z0-9])s(\d{1,2})(?=e|[^a-z0-9]|$)", title)
    if sm:
        season = int(sm.group(1))
    patterns = [
        (r"(?i)\bs\d{1,2}\s*e(\d{1,3})(?:\s*[-~&]\s*e?(\d{1,3}))?\b", False),
        (r"(?i)\b(\d{1,2})x(\d{1,3})(?:\s*[-~&]\s*(?:\d{1,2}x)?(\d{1,3}))?\b", True),
        (r"(?i)\b(?:episode|ep)\s*[._ -]?(\d{1,3})(?:\s*[-~&]\s*(\d{1,3}))?\b", False),
        (r"第\s*(\d{1,3})\s*(?:話|集)", False),
        (r"(?<![A-Za-z0-9])\[(\d{1,3})(?:\s*[~～-]\s*(\d{1,3}))?\]", False),
    ]
    for pattern, has_season in patterns:
        match = re.search(pattern, title)
        if not match:
            continue
        if has_season:
            season, start = int(match.group(1)), int(match.group(2))
            end = int(match.group(3) or start)
        else:
            start = int(match.group(1))
            end = int(match.group(2) or start) if match.lastindex and match.lastindex >= 2 else start
        if start < 1 or end < start or end - start > 100:
            continue
        return season, list(range(start, end + 1))
    return None, []


def enrich_release_from_title(release: dict) -> dict:
    title = str(release.get("title") or "")
    season, episodes = parse_release_episodes(title)
    if episodes:
        release["episode"] = release.get("episode") or episodes[0]
        release["episode_numbers"] = episodes
        release["season"] = season or 1
    if not release.get("quality"):
        match = re.search(r"(?i)\b(480|720|1080|1440|2160)p\b", title)
        if match:
            release["quality"] = match.group(0).lower()
    if not release.get("codec"):
        match = re.search(r"(?i)\b(x264|x265|h\.264|h\.265|hevc|av1)\b", title)
        if match:
            release["codec"] = match.group(1)
    if not release.get("group_name"):
        match = re.match(r"^\[([^\]]{1,50})\]", title)
        if match:
            release["group_name"] = match.group(1)
    lowered = title.lower()
    if not release.get("audio_hint"):
        if re.search(r"(?i)dual[- ]audio|multi[- ]audio", title):
            release["audio_hint"] = "Dual/Multi audio"
        elif re.search(r"(?i)\b(jpn|japanese)\b", title):
            release["audio_hint"] = "Japanese"
    if not release.get("subtitle_hint"):
        langs = []
        if re.search(r"(?i)\b(eng|english)\b", title):
            langs.append("English")
        if re.search(r"(?i)\b(jpn|japanese)\b", title):
            langs.append("Japanese")
        if "multi-sub" in lowered or "multiple subtitle" in lowered or "multi sub" in lowered:
            release["subtitle_hint"] = "Multiple subtitles" + (": " + ", ".join(langs) if langs else "")
        elif langs:
            release["subtitle_hint"] = ", ".join(langs)
    return release


def release_has_missing_episodes(conn: sqlite3.Connection, anime_id: int, title: str) -> bool:
    season, numbers = parse_release_episodes(title)
    if not numbers:
        return False
    for number in numbers:
        row = conn.execute(
            "SELECT status,has_file FROM episodes WHERE anime_id=? AND season=? AND episode=?",
            (anime_id, season or 1, number),
        ).fetchone()
        if not row or row["status"] != "available" or not row["has_file"]:
            return True
    return False


def score_release(release: dict) -> tuple[float, str]:
    title = " ".join(str(release.get(key, "")) for key in
                     ("title", "audio_hint", "subtitle_hint", "quality")).lower()
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


def release_identity(value: Any, title: str = "") -> Optional[str]:
    if not value:
        return None
    value = str(value).strip()
    if value.startswith("magnet:"):
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
        value = parsed.get("xt", [value])[0].split(":")[-1]
    match = re.search(r"\b[a-fA-F0-9]{40}\b", value)
    return match.group(0).lower() if match else (value or None)


def normalize_release(source: str, item: dict, fallback_url: str = "") -> Optional[dict]:
    title = str(item.get("title") or item.get("name") or "").strip()
    if not title:
        return None
    magnet = item.get("magnet") or item.get("magnet_link") or item.get("magnetLink")
    torrent_url = magnet or item.get("torrent_url") or item.get("torrentUrl") or item.get("download_url")
    page_url = item.get("url") or item.get("page_url") or item.get("pageUrl") or fallback_url
    identity = release_identity(item.get("id") or item.get("info_hash") or item.get("infoHash") or torrent_url or page_url, title)
    if not identity or not torrent_url:
        return None
    torrent_scheme = urllib.parse.urlparse(str(torrent_url)).scheme.lower()
    if torrent_scheme not in {"magnet", "http", "https"}:
        return None
    page_scheme = urllib.parse.urlparse(str(page_url)).scheme.lower()
    if page_scheme not in {"http", "https"}:
        page_url = fallback_url or torrent_url
    release = {
        "source": source, "source_id": identity, "title": title, "page_url": page_url or torrent_url,
        "torrent_url": torrent_url, "info_hash": release_identity(item.get("info_hash") or item.get("infoHash") or torrent_url),
        "published": item.get("published") or item.get("upload_date") or item.get("uploadDate") or "",
        "group_name": item.get("group") or item.get("release_group") or item.get("releaseGroup") or "",
        "quality": item.get("resolution") or item.get("quality") or "",
        "episode": item.get("episode") or item.get("episode_number") or item.get("episodeNumber"),
        "audio_hint": item.get("audio") or item.get("audio_language") or item.get("audioLanguage") or "",
        "subtitle_hint": item.get("subtitles") or item.get("subtitle_language") or item.get("subtitleLanguage") or "",
        "codec": item.get("codec") or "",
        "size_bytes": item.get("size_bytes") or item.get("sizeBytes") or item.get("size"),
        "seeders": item.get("seeders") or item.get("seedCount") or 0,
        "leechers": item.get("leechers") or item.get("leechCount") or 0,
    }
    release = enrich_release_from_title(release)
    release["score"], release["score_reason"] = score_release(release)
    return release


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
        release = normalize_release("Nyaa", {
            "id": info_hash or entry.get("id"), "title": title, "url": entry.get("link", NYAA_BASE),
            "torrent_url": torrent_url, "info_hash": info_hash, "published": entry.get("published", ""),
        }, NYAA_BASE)
        if release:
            releases.append(release)
    return releases


def parse_tsukihime(show: sqlite3.Row) -> list[dict]:
    try:
        response = requests.get(TSUKIHIME_BASE + "/openapi.json", timeout=10)
        if response.status_code != 200:
            return []
        spec = response.json()
        endpoint = next((
            (path, method, operation) for path, methods in spec.get("paths", {}).items()
            for method, operation in methods.items()
            if method.lower() == "get" and any(term in path.lower() for term in ("search", "release", "torrent"))
        ), None)
        if not endpoint:
            return []
        path, method, operation = endpoint
        parameter_names = {p.get("name") for p in operation.get("parameters", [])}
        query_key = next((key for key in ("q", "query", "search", "title", "name") if key in parameter_names), None)
        if not query_key:
            return []
        response = requests.request(method.upper(), TSUKIHIME_BASE + path,
                                    params={query_key: show["search_query"]}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("data", payload.get("results", []))
        return [release for item in items if (release := normalize_release("TsukiHime", item, TSUKIHIME_BASE))]
    except (requests.RequestException, ValueError, StopIteration, KeyError, TypeError):
        return []


def persist_release_search(anime_id: int, query: str) -> list[dict]:
    with DB_LOCK, closing(db()) as conn:
        show = conn.execute("SELECT * FROM anime WHERE id=?", (anime_id,)).fetchone()
        if not show:
            return []
        aliases = [row["title"] for row in conn.execute(
            "SELECT title FROM alternate_titles WHERE anime_id=?", (anime_id,)
        ).fetchall()]
        releases = parse_nyaa({"search_query": query}) + parse_tsukihime({"search_query": query})
        releases = [release for release in releases
                    if release_title_matches(show, release["title"], aliases)
                    and release_has_missing_episodes(conn, anime_id, release["title"])]
        for release in releases:
            conn.execute("""INSERT OR IGNORE INTO releases(
                anime_id,source,source_id,title,page_url,torrent_url,info_hash,published,group_name,quality,
                episode,audio_hint,subtitle_hint,codec,size_bytes,seeders,leechers,score,score_reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (anime_id, release["source"], release["source_id"], release["title"], release["page_url"],
                          release["torrent_url"], release.get("info_hash"), release.get("published"),
                          release.get("group_name"), release.get("quality"), release.get("episode"),
                          release.get("audio_hint"), release.get("subtitle_hint"), release.get("codec"),
                          release.get("size_bytes"), release.get("seeders"), release.get("leechers"),
                          release["score"], release["score_reason"], now()))
        conn.commit()
        pending = [dict(row) for row in conn.execute(
            "SELECT * FROM releases WHERE anime_id=? AND status='pending' ORDER BY score DESC,id DESC LIMIT 500",
            (anime_id,)).fetchall()]
        return [release for release in pending
                if release_title_matches(show, release["title"], aliases)
                and release_has_missing_episodes(conn, anime_id, release["title"])][:100]


def scan_sources() -> int:
    with DB_LOCK, closing(db()) as conn:
        total = 0
        for show in conn.execute("SELECT * FROM anime").fetchall():
            aliases = [row["title"] for row in conn.execute(
                "SELECT title FROM alternate_titles WHERE anime_id=?", (show["id"],)
            ).fetchall()]
            for release in parse_nyaa(show) + parse_tsukihime(show):
                if not release_title_matches(show, release["title"], aliases):
                    continue
                if not release_has_missing_episodes(conn, show["id"], release["title"]):
                    continue
                conn.execute("""INSERT OR IGNORE INTO releases
                  (anime_id,source,source_id,title,page_url,torrent_url,info_hash,published,group_name,quality,
                   episode,audio_hint,subtitle_hint,codec,size_bytes,seeders,leechers,score,score_reason,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (show["id"], release["source"], str(release["source_id"]), release["title"],
                              release["page_url"], release.get("torrent_url"), release.get("info_hash"),
                              release.get("published"), release.get("group_name"), release.get("quality"),
                              release.get("episode"), release.get("audio_hint"), release.get("subtitle_hint"),
                              release.get("codec"), release.get("size_bytes"), release.get("seeders"),
                              release.get("leechers"), release.get("score", 0), release.get("score_reason", ""), now()))
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
            if not row and provider_ids:
                provider_match = " OR ".join("(p.provider=? AND p.value=?)" for _ in provider_ids)
                row = conn.execute(
                    f"""SELECT DISTINCT a.id FROM anime a JOIN provider_ids p ON p.anime_id=a.id
                        WHERE {provider_match} LIMIT 1""",
                    tuple(value for pair in provider_ids.items() for value in pair),
                ).fetchone()
            if row:
                anime_id = row["id"]
                current = conn.execute("SELECT folder_locked,folder_name FROM anime WHERE id=?", (anime_id,)).fetchone()
                next_folder = current["folder_name"] if current["folder_locked"] else folder
                conn.execute("""UPDATE anime SET title=?, folder_name=?, search_query=?, jellyfin_id=?,
                    year=?, metadata_json=?, updated_at=? WHERE id=?""",
                             (title, next_folder, title, item.get("Id"),
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


def search_anime(query: str) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    results: dict[str, dict] = {}
    with closing(db()) as conn:
        rows = conn.execute("""SELECT DISTINCT a.* FROM anime a
            LEFT JOIN alternate_titles t ON t.anime_id=a.id
            WHERE a.title LIKE ? OR a.folder_name LIKE ? OR t.title LIKE ?
            ORDER BY a.title LIMIT 50""", (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
    for row in rows:
        item = dict(row)
        with closing(db()) as conn:
            provider_rows = conn.execute(
                "SELECT provider,value FROM provider_ids WHERE anime_id=? ORDER BY provider",
                (row["id"],),
            ).fetchall()
        item["provider_ids"] = {r["provider"]: r["value"] for r in provider_rows}
        item["provider_ids_json"] = json.dumps(item["provider_ids"], ensure_ascii=False)
        results[f"db:{row['id']}"] = item
    try:
        for item in JellyfinClient().search(query):
            provider_ids = item.get("ProviderIds") or {}
            alternate_titles = list(item.get("AlternateTitles") or [])
            alternate_titles.extend(value for value in (item.get("OriginalTitle"), item.get("SortName")) if value)
            alternate_titles = [
                value if isinstance(value, str) else value.get("Title", "")
                for value in alternate_titles
            ]
            results[f"jf:{item.get('Id')}"] = {
                "title": item.get("Name", ""), "folder_name": Path(item.get("Path", "")).name or item.get("Name", ""),
                "year": (item.get("PremiereDate") or "")[:4], "jellyfin_id": item.get("Id"),
                "metadata_json": json.dumps(item), "provider_ids": provider_ids,
                "provider_ids_json": json.dumps(provider_ids, ensure_ascii=False),
                "alternate_titles": "\n".join(dict.fromkeys(t for t in alternate_titles if t)),
            }
    except (RuntimeError, ValueError, requests.RequestException):
        pass
    try:
        for item in search_external_anime(query):
            provider_key = next(iter(item.get("provider_ids", {}).items()), ("", ""))
            matching_key = next(
                (key for key, existing in results.items()
                 if (provider_key[1] and provider_key[1] in (existing.get("provider_ids") or {}).values())
                 or normalize_title(existing.get("title", "")) == normalize_title(item["title"])),
                None,
            )
            if matching_key:
                results[matching_key].update({
                    k: v for k, v in item.items()
                    if v not in (None, "", {}, []) and not (matching_key.startswith("db:") and k == "folder_name")
                })
            else:
                key = f"external:{provider_key[0]}:{provider_key[1]}" if provider_key[1] else f"external:{normalize_title(item['title'])}"
                results[key] = item
    except (RuntimeError, ValueError, requests.RequestException):
        pass
    return list(results.values())


def search_external_anime(query: str) -> list[dict]:
    response = requests.post(
        "https://graphql.anilist.co",
        json={"query": """query ($search: String) {
            Page(perPage: 20) {
                media(search: $search, type: ANIME) {
                    id title { romaji english native userPreferred }
                    synonyms startDate { year } description
                }
            }
        }""", "variables": {"search": query}},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("errors"):
        raise RuntimeError("External anime metadata provider returned an error")
    media_items = payload.get("data", {}).get("Page", {}).get("media", [])
    results = []
    for item in media_items if isinstance(media_items, list) else []:
        title_data = item.get("title") or {}
        title = title_data.get("english") or title_data.get("romaji") or title_data.get("native") or ""
        if not title:
            continue
        alternates = []
        for value in (title_data.get("romaji"), title_data.get("english"), title_data.get("native"),
                      title_data.get("userPreferred"), *(item.get("synonyms") or [])):
            if value and value not in alternates and value != title:
                alternates.append(value)
        results.append({
            "title": title,
            "japanese_title": title_data.get("native") or "",
            "alternate_titles": "\n".join(alternates),
            "year": str((item.get("startDate") or {}).get("year") or ""),
            "synopsis": re.sub(r"<[^>]+>", "", item.get("description") or ""),
            "metadata_json": json.dumps(item),
            "provider_ids": {"AniList": str(item.get("id"))} if item.get("id") else {},
            "provider_ids_json": json.dumps(
                {"AniList": str(item.get("id"))} if item.get("id") else {}, ensure_ascii=False
            ),
            "jellyfin_id": "",
            "folder_name": title,
        })
    return results


def import_completed() -> int:
    imported = 0
    with DB_LOCK, closing(db()) as conn:
        for download in conn.execute("""SELECT d.*,r.anime_id,a.folder_name,a.title FROM downloads d
            JOIN releases r ON r.id=d.release_id JOIN anime a ON a.id=r.anime_id
            WHERE d.state='completed' AND d.imported_at IS NULL""").fetchall():
            content = Path(download["content_path"]) if download["content_path"] else None
            download_root = Path(download["save_path"] or setting(conn, "download_dir", str(DOWNLOAD_DIR))).resolve()
            if content:
                try:
                    content = content.resolve()
                    if download_root != content and download_root not in content.parents:
                        content = None
                except OSError:
                    content = None
            if not content or not content.exists():
                conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                             ("Torrent content path is unavailable; refusing to scan the shared download directory", download["id"]))
                continue
            files = [content] if content.is_file() else [
                p for p in content.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
            ]
            library_root = Path(setting(conn, "library_dir", str(LIBRARY_DIR))).resolve()
            imported_files_for_download = 0
            failed_files = []
            for source in files:
                if not path_is_within(download_root, source):
                    failed_files.append(f"{source}: path escapes download directory")
                    conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                                 ("Torrent content escapes the configured download directory", download["id"]))
                    continue
                season, episode_numbers = parse_file_episodes(source)
                if not episode_numbers:
                    failed_files.append(f"{source}: episode number is ambiguous")
                    conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                                 ("Could not identify season/episode from media filename", download["id"]))
                    continue
                try:
                    media = inspect_media(source)
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    failed_files.append(f"{source}: {exc}")
                    conn.execute("INSERT INTO media_verifications(download_id,file_path,audio_json,subtitle_json,error,verified_at) VALUES(?,?,?,?,?,?)",
                                 (download["id"], str(source), "[]", "[]", str(exc), now()))
                    conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                                 (f"Media verification failed: {exc}", download["id"]))
                    continue
                accepted = bool(media["english_ass"])
                verification = conn.execute("""INSERT INTO media_verifications(download_id,file_path,audio_json,subtitle_json,english_ass,japanese_ass,accepted,verified_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (download["id"], str(source), json.dumps(media["audio"]), json.dumps(media["subtitles"]),
                     media["english_ass"], media["japanese_ass"], accepted, now()))
                if not accepted:
                    failed_files.append(f"{source}: English ASS/SSA subtitle not found")
                    conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                                 ("English ASS/SSA subtitle not found", download["id"]))
                    continue
                destination_name = source.name
                if len(episode_numbers) > 1:
                    destination_name = f"{safe_folder(download['title'])} - S{season:02d}E{episode_numbers[0]:02d}-E{episode_numbers[-1]:02d}{source.suffix.lower()}"
                else:
                    destination_name = f"{safe_folder(download['title'])} - S{season:02d}E{episode_numbers[0]:02d}{source.suffix.lower()}"
                destination = library_destination(library_root, download["folder_name"], season, destination_name)
                if destination.exists():
                    recorded = conn.execute(
                        "SELECT 1 FROM imported_files WHERE anime_id=? AND source_path=? AND destination_path=?",
                        (download["anime_id"], str(source), str(destination)),
                    ).fetchone()
                    if recorded:
                        imported_files_for_download += 1
                        continue
                    failed_files.append(f"{source}: destination already exists")
                    conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                                 (f"Import destination already exists: {destination}", download["id"]))
                    continue
                partial = destination.with_name(destination.name + ".partial")
                try:
                    shutil.copy2(source, partial)
                    os.replace(partial, destination)
                except OSError as exc:
                    if partial.exists():
                        partial.unlink()
                    failed_files.append(f"{source}: copy failed: {exc}")
                    conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                                 (f"Import failed: {exc}", download["id"]))
                    continue
                for episode_number in episode_numbers:
                    episode = conn.execute("""SELECT id FROM episodes WHERE anime_id=? AND season=? AND episode=?""",
                                           (download["anime_id"], season, episode_number)).fetchone()
                    if not episode:
                        episode_id = conn.execute("""INSERT INTO episodes(anime_id,season,episode,status,filesystem_path,has_file)
                            VALUES(?,?,?,?,?,1)""",
                            (download["anime_id"], season, episode_number, "available", str(destination))).lastrowid
                    else:
                        episode_id = episode["id"]
                        conn.execute("UPDATE episodes SET status='available',filesystem_path=?,has_file=1 WHERE id=?",
                                     (str(destination), episode_id))
                    conn.execute("INSERT OR IGNORE INTO imported_files(anime_id,episode_id,source_path,destination_path,imported_at) VALUES(?,?,?,?,?)",
                                 (download["anime_id"], episode_id, str(source), str(destination), now()))
                imported_files_for_download += 1
            if failed_files:
                conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                             ("; ".join(failed_files), download["id"]))
                imported += imported_files_for_download
            elif imported_files_for_download:
                conn.execute("UPDATE downloads SET imported_at=?,state='imported' WHERE id=?",
                             (now(), download["id"]))
                imported += imported_files_for_download
            elif not files:
                conn.execute("UPDATE downloads SET state='needs_review',error=? WHERE id=?",
                             ("No media files were found in the torrent content path", download["id"]))
        conn.commit()
    if imported:
        try:
            JellyfinClient().refresh()
        except (RuntimeError, ValueError, requests.RequestException):
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
    """Reconcile delayed qBittorrent visibility and persist progress."""
    try:
        client = QBitClient()
        torrents = client.torrents()
    except (RuntimeError, requests.RequestException):
        return
    with DB_LOCK, closing(db()) as conn:
        rows = conn.execute("""SELECT d.*,r.info_hash,r.title release_title FROM downloads d
            JOIN releases r ON r.id=d.release_id WHERE d.imported_at IS NULL""").fetchall()
        for row in rows:
            expected_hash = (row["torrent_hash"] or row["info_hash"] or "").lower()
            torrent = next((candidate for candidate in torrents
                            if expected_hash and str(candidate.get("hash", "")).lower() == expected_hash), None)
            if not torrent and not expected_hash:
                torrent = next((candidate for candidate in torrents
                                if row["release_title"].casefold() in str(candidate.get("name", "")).casefold()), None)
            if not torrent:
                conn.execute("""UPDATE downloads SET reconcile_attempts=reconcile_attempts+1,last_checked=?,
                    state=CASE WHEN state IN ('queued','submitted') THEN 'awaiting_qbittorrent' ELSE state END
                    WHERE id=?""", (now(), row["id"]))
                continue
            content_path = torrent.get("content_path")
            if not content_path and torrent.get("hash") and hasattr(client, "torrent_properties"):
                try:
                    properties = client.torrent_properties(str(torrent["hash"]))
                    save_path = properties.get("save_path") or torrent.get("save_path") or str(QBT_SAVE_PATH)
                    if save_path and torrent.get("name"):
                        candidate = Path(save_path) / str(torrent["name"])
                        content_path = str(candidate) if candidate.exists() else None
                except (requests.RequestException, RuntimeError):
                    content_path = None
            state = "completed" if float(torrent.get("progress", 0)) >= 1 else str(torrent.get("state", "downloading"))
            conn.execute("""UPDATE downloads SET torrent_hash=?,progress=?,state=?,name=?,save_path=?,content_path=?,last_checked=?,
                completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,?) ELSE completed_at END WHERE id=?""",
                         (str(torrent.get("hash", "")) or row["torrent_hash"], float(torrent.get("progress", 0)),
                          state, torrent.get("name"), torrent.get("save_path") or str(QBT_SAVE_PATH),
                          content_path, now(), state, now(), row["id"]))
        conn.commit()
    import_completed()


PAGE = template(r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Anime Manager</title><style>
body{font:15px system-ui;background:#10121a;color:#e8eaf0;margin:0 auto;padding:24px;max-width:1250px}h1,h2{margin:.3em 0 .7em}
.muted{color:#a4a9b8}.panel{background:#1b1f2a;border:1px solid #303646;border-radius:12px;padding:16px;margin:14px 0}
input,button,select{font:inherit;padding:9px;border-radius:8px;border:1px solid #42495b;background:#10131b;color:#fff}button{background:#6654d9;border:0;cursor:pointer}
button.danger{background:#85464b}form.inline{display:inline}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px 7px;border-bottom:1px solid #303646;vertical-align:top}a{color:#b9adff}.pill{font-size:12px;color:#c7cbe0}
nav a{margin-right:15px} .stat{font-size:24px;font-weight:700}
</style></head><body><h1>Anime Manager</h1><nav><a href="/">Dashboard</a><a href="/library">My Library</a><a href="/search">Search</a><a href="/episodes">New Episodes</a><a href="/downloads">Downloads</a><a href="/history">History</a><a href="/settings">Settings</a></nav>
{% if message %}<p class="panel">{{message}}</p>{% endif %}{{ body|safe }}</body></html>""")


def page(body: str, message: str = "") -> HTMLResponse:
    token = secrets.token_urlsafe(32)
    body = re.sub(
        r"<form(\s[^>]*)?>",
        lambda match: f"<form{match.group(1) or ''}><input type=\"hidden\" name=\"csrf_token\" value=\"{token}\">",
        body,
        flags=re.IGNORECASE,
    )
    response = HTMLResponse(PAGE.render(body=body, message=message))
    if ADMIN_PASSWORD:
        response.set_cookie(CSRF_COOKIE, token, httponly=False, samesite="strict", secure=SECURE_COOKIES)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = "") -> HTMLResponse:
    if not ADMIN_PASSWORD:
        return page('<div class="panel"><h2>Authentication is not configured</h2><p>Set ADMIN_PASSWORD before exposing the dashboard.</p></div>')
    body = template("""<div class="panel"><h2>Sign in</h2>{% if error %}<p>{{error}}</p>{% endif %}
        <form method="post"><input name="password" type="password" required autofocus><button>Sign in</button></form></div>""").render(error=error)
    return page(body)


@app.post("/login")
def login(password: str = Form(...)) -> RedirectResponse:
    if not ADMIN_PASSWORD or not hmac.compare_digest(password, ADMIN_PASSWORD):
        return RedirectResponse("/login?error=Invalid%20credentials", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, session_cookie(), httponly=True, samesite="strict", secure=SECURE_COOKIES, max_age=86400)
    response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), httponly=False, samesite="strict", secure=SECURE_COOKIES)
    return response


@app.on_event("startup")
def start_scheduler() -> None:
    if ADMIN_PASSWORD and len(SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET must be at least 32 characters when authentication is enabled")
    ensure_runtime_paths()
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
        releases = [dict(row) for row in conn.execute("""SELECT r.*,a.title anime_title FROM releases r JOIN anime a ON a.id=r.anime_id
            WHERE r.status='pending' ORDER BY r.score DESC,r.id DESC LIMIT 500""").fetchall()]
        visible_releases = []
        for release in releases:
            show = conn.execute("SELECT * FROM anime WHERE id=?", (release["anime_id"],)).fetchone()
            aliases = [row["title"] for row in conn.execute(
                "SELECT title FROM alternate_titles WHERE anime_id=?", (release["anime_id"],)
            ).fetchall()]
            if show and release_title_matches(show, release["title"], aliases) and release_has_missing_episodes(
                conn, release["anime_id"], release["title"]
            ):
                release["page_url"] = safe_external_url(release["page_url"])
                visible_releases.append(release)
            if len(visible_releases) >= 50:
                break
        releases = visible_releases
    body = template("""<div class="row">{% for k,v in stats.items() %}<div class="panel"><div class="muted">{{k}}</div><div class="stat">{{v}}</div></div>{% endfor %}</div>
    <div class="panel"><div class="row"><h2 style="flex:1">Awaiting approval</h2><form method="post" action="/sync"><button>Sync now</button></form></div>
    <table><tr><th>Release</th><th>Anime</th><th>Score</th><th>Action</th></tr>{% for r in releases %}<tr><td><a href="{{r.page_url}}" target="_blank" rel="noreferrer">{{r.title}}</a><br><span class="muted">{{r.source}} · {{r.score_reason}}</span></td><td>{{r.anime_title}}</td><td>{{"%.1f"|format(r.score)}}</td><td><form class="inline" method="post" action="/releases/{{r.id}}/approve"><button>Approve</button></form><form class="inline" method="post" action="/releases/{{r.id}}/reject"><button class="danger">Reject</button></form></td></tr>{% else %}<tr><td colspan="4" class="muted">Nothing awaiting approval.</td></tr>{% endfor %}</table></div>""").render(stats=stats, releases=releases)
    return page(body)


@app.get("/library", response_class=HTMLResponse)
def library() -> HTMLResponse:
    with closing(db()) as conn:
        shows = conn.execute("SELECT a.*,count(e.id) episodes FROM anime a LEFT JOIN episodes e ON e.anime_id=a.id GROUP BY a.id ORDER BY a.title").fetchall()
    body = template("""<div class="panel"><div class="row"><h2 style="flex:1">Tracked anime</h2><form method="post" action="/library/sync"><button>Synchronize Jellyfin library</button></form></div><p class="muted">Jellyfin is the metadata source of truth; filesystem scanning verifies physical episode files.</p><form method="post" action="/anime"><input name="title" placeholder="Anime title" required><input name="folder_name" placeholder="Custom folder name"><button>Add / Track</button></form></div>
    <div class="panel"><table><tr><th>Anime</th><th>Folder</th><th>Episodes</th><th>Status</th></tr>{% for s in shows %}<tr><td>{{s.title}}</td><td>{{s.folder_name}}</td><td>{{s.episodes}}</td><td>{{s.status}}</td></tr>{% else %}<tr><td colspan="4">No anime tracked.</td></tr>{% endfor %}</table></div>""").render(shows=shows)
    return page(body)


@app.post("/anime")
def add_anime(title: str = Form(...), folder_name: str = Form(""),
              metadata_json: str = Form("{}"), provider_ids: str = Form("{}"),
              alternate_titles: str = Form(""), jellyfin_id: str = Form(""),
              year: str = Form("")) -> RedirectResponse:
    title, folder_name = title.strip(), safe_folder(folder_name.strip() or title)
    with closing(db()) as conn:
        try:
            metadata = json.loads(metadata_json or "{}")
            providers = json.loads(provider_ids or "{}")
        except ValueError as exc:
            raise HTTPException(400, "Invalid anime metadata") from exc
        existing = conn.execute(
            "SELECT id FROM anime WHERE lower(title)=lower(?) OR lower(folder_name)=lower(?)",
            (title, folder_name),
        ).fetchone()
        if not existing and providers:
            provider_match = " OR ".join("(p.provider=? AND p.value=?)" for _ in providers)
            existing = conn.execute(
                f"""SELECT DISTINCT a.id FROM anime a JOIN provider_ids p ON p.anime_id=a.id
                    WHERE {provider_match} LIMIT 1""",
                tuple(value for pair in providers.items() for value in pair),
            ).fetchone()
        if existing:
            anime_id = existing["id"]
            current = conn.execute("SELECT jellyfin_id,year,metadata_json FROM anime WHERE id=?", (anime_id,)).fetchone()
            try:
                current_metadata = json.loads(current["metadata_json"] or "{}")
            except ValueError:
                current_metadata = {}
            conn.execute(
                """UPDATE anime SET jellyfin_id=COALESCE(jellyfin_id,?), year=COALESCE(year,?),
                   metadata_json=?, updated_at=? WHERE id=?""",
                (jellyfin_id or None, int(year) if year.isdigit() else None,
                 json.dumps({**current_metadata, **metadata}), now(), anime_id),
            )
            for provider, value in providers.items():
                conn.execute("INSERT OR IGNORE INTO provider_ids(anime_id,provider,value) VALUES(?,?,?)",
                             (anime_id, str(provider), str(value)))
            for alternate in [value.strip() for value in alternate_titles.split("\n") if value.strip()]:
                conn.execute("INSERT OR IGNORE INTO alternate_titles(anime_id,title) VALUES(?,?)",
                             (anime_id, alternate))
        if not existing:
            conn.execute("""INSERT INTO anime(title,folder_name,search_query,created_at,updated_at,folder_locked)
                VALUES(?,?,?,?,?,1)""", (title, folder_name, title, now(), now()))
            anime_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE anime SET jellyfin_id=?,year=?,metadata_json=? WHERE id=?",
                         (jellyfin_id or None, int(year) if year.isdigit() else None,
                          json.dumps(metadata), anime_id))
            for provider, value in providers.items():
                conn.execute("INSERT OR IGNORE INTO provider_ids(anime_id,provider,value) VALUES(?,?,?)",
                             (anime_id, str(provider), str(value)))
            for alternate in [value.strip() for value in alternate_titles.split("\n") if value.strip()]:
                conn.execute("INSERT OR IGNORE INTO alternate_titles(anime_id,title) VALUES(?,?)",
                             (anime_id, alternate))
        conn.commit()
    return RedirectResponse("/library", status_code=303)


@app.post("/library/sync")
def library_sync() -> RedirectResponse:
    try:
        discovered = sync_jellyfin()
    except (RuntimeError, ValueError, requests.RequestException) as exc:
        raise HTTPException(502, str(exc)) from exc
    return RedirectResponse(f"/library?sync={discovered}", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_page(q: str = "", release_q: str = "", anime_id: int = 0) -> HTMLResponse:
    anime_results = search_anime(q) if q else []
    releases = []
    with closing(db()) as conn:
        tracked = conn.execute("SELECT id,title FROM anime ORDER BY title").fetchall()
    if release_q and anime_id:
        try:
            releases = persist_release_search(anime_id, release_q)
        except (requests.RequestException, ValueError):
            releases = []
    body = template("""<div class="panel"><h2>Search anime</h2>
    <form method="get"><input name="q" value="{{q}}" placeholder="Japanese, English, or alternate title" size="45"><button>Search metadata</button></form>
    <table><tr><th>Title</th><th>Year</th><th>Provider IDs</th><th>Action</th></tr>{% for item in anime_results %}<tr><td>{{item.title}}{% if item.folder_name and item.folder_name != item.title %}<br><span class="muted">{{item.folder_name}}</span>{% endif %}</td><td>{{item.year or ''}}</td><td>{{item.provider_ids or ''}}</td><td><form method="post" action="/anime"><input type="hidden" name="title" value="{{item.title}}"><input type="hidden" name="metadata_json" value="{{item.metadata_json or '{}'}}"><input type="hidden" name="provider_ids" value="{{item.provider_ids_json or '{}'}}"><input type="hidden" name="alternate_titles" value="{{item.alternate_titles or ''}}"><input type="hidden" name="jellyfin_id" value="{{item.jellyfin_id or ''}}"><input type="hidden" name="year" value="{{item.year or ''}}"><input name="folder_name" placeholder="Custom folder name"><button>Track anime</button></form></td></tr>{% else %}<tr><td colspan="4">No metadata matches.</td></tr>{% endfor %}</table></div>
    <div class="panel"><h2>Search releases</h2><form method="get"><input name="release_q" value="{{release_q}}" placeholder="Title or identifier" size="45"><select name="anime_id" required><option value="">Track releases for...</option>{% for anime in tracked %}<option value="{{anime.id}}" {% if anime.id == anime_id %}selected{% endif %}>{{anime.title}}</option>{% endfor %}</select><button>Search Nyaa and TsukiHime</button></form>
    <table><tr><th>Release</th><th>Source</th><th>Quality</th><th>Audio/subtitles</th><th>Seeders</th><th>Score</th><th>Action</th></tr>{% for r in releases %}<tr><td><a href="/releases/{{r.id}}">{{r.title}}</a></td><td>{{r.source}}</td><td>{{r.quality}}</td><td>{{r.audio_hint}} / {{r.subtitle_hint}}</td><td>{{r.seeders}}</td><td>{{"%.1f"|format(r.score)}}<br><span class="muted">{{r.score_reason}}</span></td><td><form class="inline" method="post" action="/releases/{{r.id}}/approve"><button>Approve</button></form><form class="inline" method="post" action="/releases/{{r.id}}/reject"><button class="danger">Reject</button></form></td></tr>{% else %}<tr><td colspan="7">No release matches.</td></tr>{% endfor %}</table></div>""").render(
        q=q, release_q=release_q, anime_id=anime_id, tracked=tracked, anime_results=anime_results, releases=releases)
    return page(body)


@app.get("/releases/{release_id}", response_class=HTMLResponse)
def release_detail(release_id: int) -> HTMLResponse:
    with closing(db()) as conn:
        release = conn.execute("""SELECT r.*,a.title anime_title FROM releases r JOIN anime a ON a.id=r.anime_id
            WHERE r.id=?""", (release_id,)).fetchone()
    if not release:
        raise HTTPException(404, "Release not found")
    release = dict(release)
    release["page_url"] = safe_external_url(release["page_url"])
    body = template("""<div class="panel"><h2>{{release.title}}</h2><p>{{release.anime_title}} · {{release.source}}</p>
        <p>Quality: {{release.quality}} · Audio: {{release.audio_hint}} · Subtitles: {{release.subtitle_hint}}</p>
        <p>Seeders: {{release.seeders}} · Leechers: {{release.leechers}} · Score: {{release.score}}</p>
        <p>{{release.score_reason}}</p><form class="inline" method="post" action="/releases/{{release.id}}/approve"><button>Approve</button></form>
        <form class="inline" method="post" action="/releases/{{release.id}}/reject"><button class="danger">Reject</button></form></div>""").render(release=release)
    return page(body)


@app.post("/sync")
def sync() -> RedirectResponse:
    background_sync()
    return RedirectResponse("/", status_code=303)


@app.get("/episodes", response_class=HTMLResponse)
def episodes() -> HTMLResponse:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT e.*,a.title AS anime_title FROM episodes e JOIN anime a ON a.id=e.anime_id
            ORDER BY e.air_date DESC LIMIT 100""").fetchall()
    body = template("""<div class="panel"><h2>Episodes</h2><table><tr><th>Anime</th><th>Episode</th><th>Title</th><th>Status</th></tr>{% for e in rows %}<tr><td>{{e.anime_title}}</td><td>S{{"%02d"|format(e.season or 1)}}E{{"%02d"|format(e.episode)}}</td><td>{{e.title}}</td><td>{{e.status}}</td></tr>{% else %}<tr><td colspan="4">No episode metadata yet. Run Sync now.</td></tr>{% endfor %}</table></div>""").render(rows=rows)
    return page(body)


@app.get("/downloads", response_class=HTMLResponse)
def downloads() -> HTMLResponse:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT d.*,r.title FROM downloads d JOIN releases r ON r.id=d.release_id ORDER BY d.id DESC""").fetchall()
    body = template("""<div class="panel"><h2>Downloads</h2><table><tr><th>Release</th><th>State</th><th>Progress</th><th>Verification</th></tr>{% for d in rows %}<tr><td>{{d.title}}</td><td>{{d.state}}</td><td>{{"%.1f"|format(d.progress*100)}}%</td><td>{{d.error or "pending"}}</td></tr>{% else %}<tr><td colspan="4">No approved downloads.</td></tr>{% endfor %}</table></div>""").render(rows=rows)
    return page(body)


@app.get("/history", response_class=HTMLResponse)
def history() -> HTMLResponse:
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM scan_history ORDER BY id DESC LIMIT 50").fetchall()
    body = template("""<div class="panel"><h2>History</h2><table><tr><th>Kind</th><th>Status</th><th>Message</th><th>Finished</th></tr>{% for r in rows %}<tr><td>{{r.kind}}</td><td>{{r.status}}</td><td>{{r.message}}</td><td>{{r.finished_at}}</td></tr>{% endfor %}</table></div>""").render(rows=rows)
    return page(body)


@app.get("/settings", response_class=HTMLResponse)
def settings() -> HTMLResponse:
    with closing(db()) as conn:
        values = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM settings")}
    body = template("""<div class="panel"><h2>Settings</h2><form method="post"><p>Jellyfin URL <input name="jellyfin_url" value="{{values.get('jellyfin_url','')}}" size="45"></p><p>Jellyfin API key <input name="jellyfin_api_key" type="password" placeholder="unchanged if blank" size="45"></p><p>qBittorrent URL <input name="qbittorrent_url" value="{{values.get('qbittorrent_url','')}}" size="45"></p><p>qBittorrent username <input name="qbittorrent_username" value="{{values.get('qbittorrent_username','')}}" size="25"></p><p>qBittorrent password <input name="qbittorrent_password" type="password" placeholder="unchanged if blank" size="25"></p><p>Library path <input name="library_dir" value="{{values.get('library_dir','/ivan/Shows')}}" size="35"></p><p>Download path <input name="download_dir" value="{{values.get('download_dir','/downloads')}}" size="35"></p><button>Save settings</button></form><p class="muted">Secrets are never returned in frontend responses. Environment variables take precedence.</p></div>""").render(values=values)
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
    client = QBitClient()
    save_path = QBT_SAVE_PATH
    save_path.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, closing(db()) as conn:
        release = conn.execute("SELECT r.*,d.id download_id FROM releases r LEFT JOIN downloads d ON d.release_id=r.id WHERE r.id=?", (release_id,)).fetchone()
        if not release:
            raise HTTPException(404, "Release not found")
        if release["status"] != "pending" or release["download_id"]:
            return RedirectResponse("/", status_code=303)
        if not release["torrent_url"]:
            raise HTTPException(400, "Release has no torrent or magnet URL")
        show = conn.execute("SELECT * FROM anime WHERE id=?", (release["anime_id"],)).fetchone()
        aliases = [row["title"] for row in conn.execute(
            "SELECT title FROM alternate_titles WHERE anime_id=?", (release["anime_id"],)
        ).fetchall()]
        if not show or not release_title_matches(show, release["title"], aliases):
            raise HTTPException(400, "Release title does not match this anime; refusing download")
        if not release_has_missing_episodes(conn, release["anime_id"], release["title"]):
            raise HTTPException(400, "Release has no confirmed missing episodes; refusing download")
        try:
            info_hash = release["info_hash"] or release_identity(release["torrent_url"], release["title"])
            existing = client.find(info_hash=info_hash)
            if not existing:
                client.add(release["torrent_url"], str(save_path))
        except (RuntimeError, requests.RequestException) as exc:
            raise HTTPException(502, str(exc)) from exc
        conn.execute("UPDATE releases SET status='approved' WHERE id=?", (release_id,))
        conn.execute("""INSERT INTO downloads(release_id,torrent_hash,state,save_path,content_path,approved_at,submission_key,last_checked)
            VALUES(?,?,?,?,?,?,?,?)""",
                     (release_id, (existing or {}).get("hash") or info_hash, "submitted" if not existing else
                      str(existing.get("state", "queued")), str(save_path), (existing or {}).get("content_path"),
                      now(), info_hash, now()))
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
