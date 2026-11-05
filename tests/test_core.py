import sqlite3
import json
from pathlib import Path

import pytest

from app import main


def test_safe_folder_rejects_traversal():
    with pytest.raises(ValueError):
        main.safe_folder('../outside')
    assert main.safe_folder('Steins;Gate') == 'Steins;Gate'


def test_jellyfin_path_mapping(monkeypatch):
    monkeypatch.setattr(main, 'JELLYFIN_PATH_PREFIXES', ['/mnt/Jellyfin/JellyfinData/JP-BEAN/Shows=/ivan/Shows'])
    assert main.map_jellyfin_path('/mnt/Jellyfin/JellyfinData/JP-BEAN/Shows/Anime') == Path('/ivan/Shows/Anime')
    assert main.map_jellyfin_path('/ivan/Shows/Anime') == Path('/ivan/Shows/Anime')


def test_score_prioritizes_multiaudio_japanese_1080p():
    score, reason = main.score_release({'title': '[Group] Show - 01 [Dual Audio][1080p][ENG]'})
    assert score >= 75
    assert 'multi-audio' in reason
    assert '1080p' in reason


def test_release_parse_uses_magnet_when_hash_only(monkeypatch):
    show = {'search_query': 'Show'}
    entries = [{'title': '[Group] Show 01 1080p', 'id': 'x', 'link': 'https://nyaa.si/view/1',
                'nyaa_infohash': 'a' * 40, 'links': []}]
    Feed = type("Feed", (), {"feed": {}, "entries": entries})
    monkeypatch.setattr(main.feedparser, 'parse', lambda _: Feed())
    monkeypatch.setattr(main.requests, 'get', lambda *a, **k: type('R', (), {'content': b''})())
    result = main.parse_nyaa(show)
    assert result[0]['torrent_url'].startswith('magnet:')


def test_schema_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    conn = main.db()
    conn.execute("INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
                 ('Show', 'Show', 'Show', main.now(), main.now()))
    conn.commit(); conn.close()
    conn = main.db()
    assert conn.execute('SELECT title FROM anime').fetchone()['title'] == 'Show'
    conn.close()


def test_jellyfin_client_connectivity_and_shows_library(monkeypatch):
    calls = []

    class Response:
        content = b'[]'

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"Name": "Movies", "CollectionType": "movies", "ItemId": "m"},
                {"Name": "Shows", "CollectionType": "tvshows", "ItemId": "s"},
            ]

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr(main.requests, "request", request)
    client = main.JellyfinClient("http://jellyfin:8096", "secret")
    assert client.shows_library()["ItemId"] == "s"
    assert calls[0][2]["headers"]["X-Emby-Token"] == "secret"


def test_jellyfin_sync_discovers_series_and_scans_files(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    library = tmp_path / "Shows"
    series_path = library / "My Anime"
    series_path.mkdir(parents=True)
    episode_file = series_path / "My Anime - S01E01.mkv"
    episode_file.write_bytes(b"test")
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "JELLYFIN_PATH_PREFIXES", [f"/reported/Shows={library}"])

    class FakeJellyfin:
        def series(self):
            return [{
                "Id": "series-1", "Name": "My Anime", "Path": "/reported/Shows/My Anime",
                "ProviderIds": {"Tvdb": "123"}, "OriginalTitle": "マイアニメ",
                "PremiereDate": "2024-01-01",
            }]

        def episodes(self, series_id):
            return [{"Id": "episode-1", "ParentIndexNumber": 1, "IndexNumber": 1,
                     "Name": "First", "Path": "/reported/Shows/My Anime/My Anime - S01E01.mkv"}]

    monkeypatch.setattr(main, "JellyfinClient", FakeJellyfin)
    assert main.sync_jellyfin() == 1
    with main.db() as conn:
        anime = conn.execute("SELECT * FROM anime").fetchone()
        episode = conn.execute("SELECT * FROM episodes").fetchone()
        provider = conn.execute("SELECT * FROM provider_ids").fetchone()
        alternate = conn.execute("SELECT * FROM alternate_titles").fetchone()
    assert anime["jellyfin_id"] == "series-1"
    assert provider["value"] == "123"
    assert alternate["title"] == "マイアニメ"
    assert episode["has_file"] == 1
    assert episode["filesystem_path"] == str(episode_file)


def test_jellyfin_sync_updates_existing_manual_anime_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with main.db() as conn:
        conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("My Anime", "My Anime", "My Anime", main.now(), main.now()),
        )
        conn.commit()

    class FakeJellyfin:
        def series(self):
            return [{"Id": "series-1", "Name": "My Anime", "Path": "/ivan/Shows/My Anime"}]

        def episodes(self, series_id):
            return []

    monkeypatch.setattr(main, "JellyfinClient", FakeJellyfin)
    assert main.sync_jellyfin() == 1
    with main.db() as conn:
        assert conn.execute("SELECT count(*) FROM anime").fetchone()[0] == 1
        assert conn.execute("SELECT jellyfin_id FROM anime").fetchone()[0] == "series-1"
