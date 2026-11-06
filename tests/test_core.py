from pathlib import Path
import asyncio
import re
import subprocess

import pytest
import requests

from app import main
from fastapi.testclient import TestClient


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


def test_tsukihime_normalizes_schema_and_score(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "paths": {"/releases/search": {
                    "get": {"parameters": [{"name": "q"}]}
                }}
            }

    class Results:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{
                "title": "[Group] Anime 01",
                "magnet": "magnet:?xt=urn:btih:" + "b" * 40,
                "info_hash": "b" * 40, "resolution": "1080p",
                "audio": "dual audio", "subtitles": "English ASS",
                "seeders": 42, "leechers": 3,
            }]}

    monkeypatch.setattr(main.requests, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(main.requests, "request", lambda *args, **kwargs: Results())
    results = main.parse_tsukihime({"search_query": "Anime"})
    assert len(results) == 1
    assert results[0]["source"] == "TsukiHime"
    assert results[0]["source_id"] == "b" * 40
    assert results[0]["torrent_url"].startswith("magnet:")
    assert results[0]["score"] > 0
    assert "1080p" in results[0]["score_reason"]


def test_monitor_reconciles_delayed_torrent_visibility(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        release_id = conn.execute(
            """INSERT INTO releases(anime_id,source,source_id,title,page_url,torrent_url,info_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (anime_id, "Nyaa", "hash", "Anime 01", "https://example.test", "magnet:?xt=urn:btih:" + "c" * 40, "c" * 40, main.now()),
        ).lastrowid
        conn.execute("INSERT INTO downloads(release_id,torrent_hash,state,save_path) VALUES(?,?,?,?)",
                     (release_id, "c" * 40, "submitted", str(tmp_path)))
        conn.commit()

    class FakeQbit:
        def torrents(self):
            return [{"hash": "c" * 40, "name": "Anime 01", "progress": 1, "state": "uploading",
                     "save_path": str(tmp_path)}]

    monkeypatch.setattr(main, "QBitClient", FakeQbit)
    monkeypatch.setattr(main, "import_completed", lambda: 0)
    main.monitor_downloads()
    with main.db() as conn:
        row = conn.execute("SELECT state,progress,completed_at FROM downloads").fetchone()
    assert row["state"] == "completed"
    assert row["progress"] == 1
    assert row["completed_at"]


def test_approval_does_not_duplicate_visible_torrent(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "QBT_SAVE_PATH", tmp_path / "downloads" / "anime")
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        conn.execute(
            """INSERT INTO releases(anime_id,source,source_id,title,page_url,torrent_url,info_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (anime_id, "Nyaa", "hash", "Anime 01", "https://example.test",
             "magnet:?xt=urn:btih:" + "d" * 40, "d" * 40, main.now()),
        )
        conn.commit()
    class FakeQbit:
        added = False

        def find(self, info_hash=None):
            return {"hash": "d" * 40, "state": "downloading"}

        def add(self, *args, **kwargs):
            self.added = True
            raise AssertionError("duplicate submission")
    fake = FakeQbit()
    monkeypatch.setattr(main, "QBitClient", lambda: fake)
    response = main.approve(1)
    assert response.status_code == 303
    assert fake.added is False
    with main.db() as conn:
        assert conn.execute("SELECT status FROM releases").fetchone()[0] == "approved"
        assert conn.execute("SELECT count(*) FROM downloads").fetchone()[0] == 1


def test_import_supports_season_and_multi_episode_without_overwrite(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    download_root = tmp_path / "download"
    library_root = tmp_path / "library"
    download_root.mkdir()
    source = download_root / "Anime.S02E03-E04.mkv"
    source.write_bytes(b"video")
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "LIBRARY_DIR", library_root)
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        release_id = conn.execute(
            """INSERT INTO releases(anime_id,source,source_id,title,page_url,created_at)
               VALUES(?,?,?,?,?,?)""",
            (anime_id, "Nyaa", "release", "Anime", "https://example.test", main.now()),
        ).lastrowid
        conn.execute("INSERT INTO downloads(release_id,state,save_path,content_path) VALUES(?,?,?,?)",
                     (release_id, "completed", str(download_root), str(source)))
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("library_dir", str(library_root)))
        conn.commit()
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "ass"}], "english_ass": True, "japanese_ass": True})
    class FakeJellyfin:
        def refresh(self):
            return None
    monkeypatch.setattr(main, "JellyfinClient", FakeJellyfin)
    assert main.import_completed() == 1
    destination = library_root / "Anime" / "Season 02" / "Anime - S02E03-E04.mkv"
    assert destination.exists()
    assert source.exists()
    with main.db() as conn:
        rows = conn.execute("SELECT season,episode,has_file FROM episodes ORDER BY episode").fetchall()
        assert [(r["season"], r["episode"], r["has_file"]) for r in rows] == [(2, 3, 1), (2, 4, 1)]


def test_import_rejects_non_ass_subtitles(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    source_dir = tmp_path / "download"
    source_dir.mkdir()
    source = source_dir / "Anime - S01E01.mkv"
    source.write_bytes(b"video")
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        release_id = conn.execute(
            "INSERT INTO releases(anime_id,source,source_id,title,page_url,created_at) VALUES(?,?,?,?,?,?)",
            (anime_id, "Nyaa", "release", "Anime", "https://example.test", main.now()),
        ).lastrowid
        conn.execute("INSERT INTO downloads(release_id,state,save_path,content_path) VALUES(?,?,?,?)",
                     (release_id, "completed", str(source_dir), str(source_dir)))
        conn.commit()
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "subrip", "tags": {"language": "eng"}}],
        "english_ass": False, "japanese_ass": False})
    assert main.import_completed() == 0
    with main.db() as conn:
        row = conn.execute("SELECT state,error FROM downloads").fetchone()
    assert row["state"] == "needs_review"
    assert "ASS/SSA" in row["error"]


def test_import_uses_each_torrent_content_path_for_two_anime(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    first = download_root / "torrent-one"
    second = download_root / "torrent-two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    first_file = first / "Alpha - S01E01.mkv"
    second_file = second / "Beta - S01E02.mkv"
    first_file.write_bytes(b"alpha")
    second_file.write_bytes(b"beta")
    with main.db() as conn:
        anime_ids = []
        release_ids = []
        for title in ("Alpha", "Beta"):
            anime_id = conn.execute(
                "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
                (title, title, title, main.now(), main.now()),
            ).lastrowid
            release_id = conn.execute(
                "INSERT INTO releases(anime_id,source,source_id,title,page_url,created_at) VALUES(?,?,?,?,?,?)",
                (anime_id, "Nyaa", title, title, "https://example.test", main.now()),
            ).lastrowid
            anime_ids.append(anime_id)
            release_ids.append(release_id)
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("library_dir", str(library_root)))
        conn.execute("INSERT INTO downloads(release_id,state,save_path,content_path) VALUES(?,?,?,?)",
                     (release_ids[0], "completed", str(download_root), str(first)))
        conn.execute("INSERT INTO downloads(release_id,state,save_path,content_path) VALUES(?,?,?,?)",
                     (release_ids[1], "completed", str(download_root), str(second)))
        conn.commit()
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "ass"}], "english_ass": True, "japanese_ass": False})
    monkeypatch.setattr(main, "JellyfinClient", lambda: type("J", (), {"refresh": lambda self: None})())
    assert main.import_completed() == 2
    assert (library_root / "Alpha" / "Season 01" / "Alpha - S01E01.mkv").exists()
    assert (library_root / "Beta" / "Season 01" / "Beta - S01E02.mkv").exists()
    assert not (library_root / "Alpha" / "Season 01" / "Beta - S01E02.mkv").exists()


def _create_import_download(tmp_path, files):
    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    download_root.mkdir()
    for name in files:
        (download_root / name).write_bytes(b"video")
    main.DB_PATH = str(tmp_path / "db.sqlite3")
    main.DATA_DIR = tmp_path
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        release_id = conn.execute(
            "INSERT INTO releases(anime_id,source,source_id,title,page_url,created_at) VALUES(?,?,?,?,?,?)",
            (anime_id, "Nyaa", "release", "Anime", "https://example.test", main.now()),
        ).lastrowid
        conn.execute("INSERT INTO downloads(release_id,state,save_path,content_path) VALUES(?,?,?,?)",
                     (release_id, "completed", str(download_root), str(download_root)))
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("library_dir", str(library_root)))
        conn.commit()
    return download_root, library_root


def test_partial_import_marks_review_and_records_failed_file(tmp_path, monkeypatch):
    download_root, library_root = _create_import_download(
        tmp_path, ["Anime - S01E01.mkv", "Anime - S01E02.mkv"])
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "ass"}], "english_ass": True, "japanese_ass": False})
    original_copy = main.shutil.copy2
    def copy_with_failure(source, destination):
        if source.name.endswith("E02.mkv"):
            raise OSError("simulated copy failure")
        return original_copy(source, destination)
    monkeypatch.setattr(main.shutil, "copy2", copy_with_failure)
    assert main.import_completed() == 1
    with main.db() as conn:
        row = conn.execute("SELECT state,imported_at,error FROM downloads").fetchone()
        assert row["state"] == "needs_review"
        assert row["imported_at"] is None
        assert "E02.mkv" in row["error"]
        assert conn.execute("SELECT count(*) FROM imported_files").fetchone()[0] == 1
    assert (library_root / "Anime" / "Season 01" / "Anime - S01E01.mkv").exists()
    assert not (library_root / "Anime" / "Season 01" / "Anime - S01E02.mkv").exists()


def test_all_failed_imports_never_become_already_imported(tmp_path, monkeypatch):
    download_root, _ = _create_import_download(
        tmp_path, ["Anime - S01E01.mkv", "Anime - S01E02.mkv"])
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "ass"}], "english_ass": True, "japanese_ass": False})
    monkeypatch.setattr(main.shutil, "copy2", lambda source, destination: (_ for _ in ()).throw(OSError("copy failed")))
    assert main.import_completed() == 0
    with main.db() as conn:
        row = conn.execute("SELECT state,imported_at,error FROM downloads").fetchone()
    assert row["state"] == "needs_review"
    assert row["imported_at"] is None
    assert "copy failed" in row["error"]


def test_retry_partial_import_does_not_duplicate_records(tmp_path, monkeypatch):
    download_root, library_root = _create_import_download(tmp_path, ["Anime - S01E01.mkv", "Anime - S01E02.mkv"])
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "ass"}], "english_ass": True, "japanese_ass": False})
    original_copy = main.shutil.copy2
    monkeypatch.setattr(main.shutil, "copy2", lambda source, destination:
                        (_ for _ in ()).throw(OSError("first attempt")) if source.name.endswith("E02.mkv")
                        else original_copy(source, destination))
    assert main.import_completed() == 1
    monkeypatch.setattr(main.shutil, "copy2", original_copy)
    with main.db() as conn:
        conn.execute("UPDATE downloads SET state='completed' WHERE state='needs_review'")
        conn.commit()
    assert main.import_completed() == 2
    with main.db() as conn:
        row = conn.execute("SELECT state,imported_at FROM downloads").fetchone()
        assert row["state"] == "imported"
        assert row["imported_at"]
        assert conn.execute("SELECT count(*) FROM imported_files").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 2
    assert (library_root / "Anime" / "Season 01" / "Anime - S01E02.mkv").exists()


def test_import_destination_collision_requires_review_without_overwrite(tmp_path, monkeypatch):
    download_root, library_root = _create_import_download(tmp_path, ["Anime - S01E01.mkv"])
    destination = library_root / "Anime" / "Season 01" / "Anime - S01E01.mkv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"unrelated existing media")
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "ass"}], "english_ass": True, "japanese_ass": False})
    assert main.import_completed() == 0
    assert destination.read_bytes() == b"unrelated existing media"
    with main.db() as conn:
        row = conn.execute("SELECT state,error FROM downloads").fetchone()
        assert row["state"] == "needs_review"
        assert "already exists" in row["error"]
        assert conn.execute("SELECT count(*) FROM imported_files").fetchone()[0] == 0


def test_external_anime_search_normalizes_anilist_metadata(monkeypatch):
    monkeypatch.setattr(main, "JellyfinClient", lambda: type("J", (), {"search": lambda self, query: []})())
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": {"Page": {"media": [{
                "id": 123, "title": {"english": "Steins;Gate", "romaji": "Steins;Gate",
                "native": "シュタインズ・ゲート", "userPreferred": "Steins;Gate"},
                "synonyms": ["シュタゲ"], "startDate": {"year": 2011},
                "description": "<p>A story.</p>",
            }]}}}
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: Response())
    results = main.search_anime("Steins")
    assert results[0]["provider_ids"] == {"AniList": "123"}
    assert results[0]["japanese_title"] == "シュタインズ・ゲート"
    assert "シュタゲ" in results[0]["alternate_titles"]
    assert results[0]["synopsis"] == "A story."


def test_external_search_deduplicates_provider_result_with_local_title(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with main.db() as conn:
        conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Steins;Gate", "Custom", "Steins", main.now(), main.now()),
        )
        conn.commit()
    monkeypatch.setattr(main, "JellyfinClient", lambda: type("J", (), {"search": lambda self, query: []})())
    monkeypatch.setattr(main, "search_external_anime", lambda query: [{
        "title": "Steins;Gate", "provider_ids": {"AniList": "123"},
        "alternate_titles": "シュタゲ", "metadata_json": "{}", "folder_name": "Steins;Gate",
    }])
    results = main.search_anime("Steins")
    assert len(results) == 1
    assert results[0]["folder_name"] == "Custom"


def test_search_form_serializes_anilist_provider_ids_as_json(monkeypatch):
    monkeypatch.setattr(main, "search_anime", lambda query: [{
        "title": "External Anime", "folder_name": "External Anime", "year": "2024",
        "provider_ids": {"AniList": "123"}, "provider_ids_json": '{"AniList": "123"}',
        "metadata_json": '{"id":123}', "alternate_titles": "日本語タイトル",
        "jellyfin_id": "",
    }])
    response = main.search_page(q="External")
    html = response.body.decode()
    assert 'name="provider_ids" value="{&#34;AniList&#34;: &#34;123&#34;}"' in html
    assert "{'AniList': '123'}" not in html


def test_tracking_anilist_result_persists_metadata_and_does_not_download(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    response = main.add_anime(
        title="External Anime", folder_name="Custom Folder",
        metadata_json='{"id":123,"description":"Synopsis"}',
        provider_ids='{"AniList":"123"}',
        alternate_titles="日本語タイトル\nAlt Title", jellyfin_id="", year="2024",
    )
    assert response.status_code == 303
    with main.db() as conn:
        anime = conn.execute("SELECT * FROM anime").fetchone()
        providers = conn.execute("SELECT provider,value FROM provider_ids").fetchall()
        titles = conn.execute("SELECT title FROM alternate_titles ORDER BY title").fetchall()
        assert anime["folder_name"] == "Custom Folder"
        assert anime["year"] == 2024
        assert '"id": 123' in anime["metadata_json"]
        assert [(r["provider"], r["value"]) for r in providers] == [("AniList", "123")]
        assert [r["title"] for r in titles] == ["Alt Title", "日本語タイトル"]
        assert conn.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0


def test_search_anime_external_empty_timeout_and_error_are_graceful(monkeypatch):
    monkeypatch.setattr(main, "JellyfinClient", lambda: type("J", (), {"search": lambda self, query: []})())
    monkeypatch.setattr(main, "search_external_anime", lambda query: [])
    assert main.search_anime("unknown") == []
    monkeypatch.setattr(main, "search_external_anime", lambda query: (_ for _ in ()).throw(
        requests.Timeout("timeout")))
    assert main.search_anime("unknown") == []
    monkeypatch.setattr(main, "search_external_anime", lambda query: (_ for _ in ()).throw(
        requests.RequestException("error")))
    assert main.search_anime("unknown") == []


def test_episode_parser_rejects_ambiguous_numbers_and_ignores_resolution():
    assert main.parse_file_episodes(Path("Show - S01E03 [1080p][x265].mkv")) == (1, [3])
    assert main.parse_file_episodes(Path("Show - EP04 [720p].mkv")) == (1, [4])
    assert main.parse_file_episodes(Path("Show 2024 1080p x265.mkv")) == (1, [])


def test_authentication_and_csrf_helpers(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "test-password")
    cookie = main.session_cookie()
    assert main.valid_session(cookie)
    assert not main.valid_session("invalid")
    from starlette.requests import Request
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                       "query_string": b"", "scheme": "http", "client": ("test", 1),
                       "server": ("test", 80), "root_path": ""})
    request._cookies = {main.CSRF_COOKIE: "good"}
    with pytest.raises(main.HTTPException):
        main.require_csrf(request, "bad")


def test_rendered_forms_preserve_attributes_and_include_csrf(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "test-password")
    response = main.page('<form method="post" action="/upload" enctype="multipart/form-data"><input name="file"></form>')
    html = response.body.decode()
    assert '<form method="post" action="/upload" enctype="multipart/form-data">' in html
    assert re.search(r'<input type="hidden" name="csrf_token" value="[^"]+">', html)
    assert main.CSRF_COOKIE in response.headers.get("set-cookie", "")


def test_login_post_requires_csrf_but_valid_token_passes_middleware(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "test-password")

    def request(body, cookie):
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        return __import__("starlette.requests", fromlist=["Request"]).Request({
            "type": "http", "method": "POST", "path": "/login", "headers": [
                (b"cookie", cookie.encode())
            ], "query_string": b"", "scheme": "http", "client": ("test", 1),
            "server": ("test", 80), "root_path": "",
        }, receive=receive)

    async def next_response(request):
        return main.HTMLResponse("ok")

    token = "csrf-token"
    blocked = asyncio.run(main.security_middleware(
        request(b"password=test-password", f"{main.CSRF_COOKIE}={token}"), next_response))
    assert blocked.status_code == 403
    allowed = asyncio.run(main.security_middleware(
        request(f"password=test-password&csrf_token={token}".encode(), f"{main.CSRF_COOKIE}={token}"),
        next_response))
    assert allowed.status_code == 200


def test_http_security_middleware_blocks_anonymous_and_csrfless_requests(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "test-password")

    def request(method, path, cookie=""):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        return __import__("starlette.requests", fromlist=["Request"]).Request({
            "type": "http", "method": method, "path": path, "headers": [
                (b"cookie", cookie.encode())
            ], "query_string": b"", "scheme": "http", "client": ("test", 1),
            "server": ("test", 80), "root_path": "",
        }, receive=receive)

    async def next_response(request):
        return main.HTMLResponse("ok")

    anonymous = asyncio.run(main.security_middleware(request("GET", "/settings"), next_response))
    assert anonymous.status_code == 303
    session = main.session_cookie()
    csrf = "csrf"
    blocked = asyncio.run(main.security_middleware(
        request("POST", "/settings", f"{main.SESSION_COOKIE}={session}; {main.CSRF_COOKIE}={csrf}"),
        next_response,
    ))
    assert blocked.status_code == 403


def test_qbittorrent_uses_configured_save_path(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "QBT_SAVE_PATH", tmp_path / "anime")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    captured = {}

    class Response:
        status_code = 200
        text = "Ok."

    class Session:
        def post(self, url, **kwargs):
            captured.update(kwargs["data"])
            return Response()

    client = main.QBitClient()
    monkeypatch.setattr(client, "session", lambda: Session())
    client.add("magnet:?xt=urn:btih:" + "a" * 40)
    assert captured["savepath"] == str(tmp_path / "anime")


def test_ffprobe_failure_needs_review(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    download_root = tmp_path / "download"
    download_root.mkdir()
    source = download_root / "Anime - S01E01.mkv"
    source.write_bytes(b"not media")
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        release_id = conn.execute(
            "INSERT INTO releases(anime_id,source,source_id,title,page_url,created_at) VALUES(?,?,?,?,?,?)",
            (anime_id, "Nyaa", "release", "Anime", "https://example.test", main.now()),
        ).lastrowid
        conn.execute("INSERT INTO downloads(release_id,state,save_path,content_path) VALUES(?,?,?,?)",
                     (release_id, "completed", str(download_root), str(source)))
        conn.commit()
    monkeypatch.setattr(main, "inspect_media", lambda path: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, "ffprobe")))
    assert main.import_completed() == 0
    with main.db() as conn:
        row = conn.execute("SELECT state FROM downloads").fetchone()
    assert row["state"] == "needs_review"


def test_custom_folder_survives_jellyfin_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with main.db() as conn:
        conn.execute(
            "INSERT INTO anime(title,folder_name,folder_locked,search_query,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("Official Title", "Custom Folder", 1, "Official Title", main.now(), main.now()),
        )
        conn.commit()

    class FakeJellyfin:
        def series(self):
            return [{"Id": "series-1", "Name": "Official Title", "Path": "/ivan/Shows/Official Title"}]

        def episodes(self, series_id):
            return []

    monkeypatch.setattr(main, "JellyfinClient", FakeJellyfin)
    assert main.sync_jellyfin() == 1
    with main.db() as conn:
        row = conn.execute("SELECT folder_name,jellyfin_id FROM anime").fetchone()
    assert row["folder_name"] == "Custom Folder"
    assert row["jellyfin_id"] == "series-1"


def test_first_visit_login_and_authenticated_post_csrf_flow(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "test-password")
    monkeypatch.setattr(main, "SESSION_SECRET", "s" * 32)
    monkeypatch.setattr(main, "sync_jellyfin", lambda: 0)
    client = TestClient(main.app)

    login_page = client.get("/login")
    assert login_page.status_code == 200
    cookie_token = client.cookies.get(main.CSRF_COOKIE)
    form_token = re.search(
        r'name="csrf_token" value="([^"]+)"', login_page.text
    ).group(1)
    assert cookie_token == form_token

    login = client.post("/login", data={"password": "test-password", "csrf_token": form_token}, follow_redirects=False)
    assert login.status_code == 303
    assert client.cookies.get(main.SESSION_COOKIE)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    csrf = client.cookies.get(main.CSRF_COOKIE)
    sync = client.post("/library/sync", data={"csrf_token": csrf}, follow_redirects=False)
    assert sync.status_code == 303

    rejected = client.post("/library/sync", data={"csrf_token": "wrong"}, follow_redirects=False)
    assert rejected.status_code == 403


def test_scan_series_files_uses_safe_episode_parser(tmp_path):
    (tmp_path / "Show - S01E01 [1080p][x265].mkv").write_bytes(b"")
    (tmp_path / "Show - 01x02 [720p].mkv").write_bytes(b"")
    (tmp_path / "Show - 2024 1080p x265.mkv").write_bytes(b"")
    (tmp_path / "Show - 12 13.mkv").write_bytes(b"")
    scanned = main.scan_series_files(tmp_path)
    assert (1, 1) in scanned
    assert (1, 2) in scanned
    assert all(path.name != "Show - 2024 1080p x265.mkv" for path in scanned.values())
    assert all(path.name != "Show - 12 13.mkv" for path in scanned.values())


def test_tracking_preserves_alternate_titles_and_matches_provider_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with main.db() as conn:
        first = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at,folder_locked) VALUES(?,?,?,?,?,1)",
            ("Existing", "Custom Folder", "Existing", main.now(), main.now()),
        ).lastrowid
        conn.execute("INSERT INTO provider_ids(anime_id,provider,value) VALUES(?,?,?)", (first, "mal", "wrong"))
        other = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at,folder_locked) VALUES(?,?,?,?,?,1)",
            ("Other", "Other", "Other", main.now(), main.now()),
        ).lastrowid
        conn.execute("INSERT INTO provider_ids(anime_id,provider,value) VALUES(?,?,?)", (other, "anilist", "42"))
        conn.commit()

    response = main.add_anime(
        title="New Title", folder_name="New Custom", metadata_json='{"genres":["anime"]}',
        provider_ids='{"mal":"42","anilist":"999"}', alternate_titles="日本語タイトル\nRomanized",
        jellyfin_id="jf-1", year="2025",
    )
    assert response.status_code == 303
    with main.db() as conn:
        assert conn.execute("SELECT count(*) FROM anime").fetchone()[0] == 3

    main.add_anime(
        title="Existing", folder_name="Should Not Replace", metadata_json='{"overview":"added"}',
        provider_ids='{"mal":"123"}', alternate_titles="Alias", jellyfin_id="", year="",
    )
    with main.db() as conn:
        row = conn.execute("SELECT folder_name,metadata_json FROM anime WHERE title='Existing'").fetchone()
        assert row["folder_name"] == "Custom Folder"
        assert "overview" in row["metadata_json"]
        assert conn.execute("SELECT title FROM alternate_titles WHERE anime_id=?", (first,)).fetchone()["title"] == "Alias"


def test_runtime_permission_diagnostics_name_exact_failing_path(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    qbit_dir = tmp_path / "downloads" / "anime"
    library_dir = tmp_path / "ivan" / "Shows"
    library_dir.mkdir(parents=True)
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "QBT_SAVE_PATH", qbit_dir)
    monkeypatch.setattr(main, "LIBRARY_DIR", library_dir)
    main.ensure_runtime_paths()
    assert not list(data_dir.glob(".anime-manager-write-test-*"))
    assert not list(qbit_dir.glob(".anime-manager-write-test-*"))
    assert not list(library_dir.glob(".anime-manager-write-test-*"))

    bad_data = tmp_path / "not-a-directory"
    bad_data.write_text("file")
    monkeypatch.setattr(main, "DATA_DIR", bad_data)
    with pytest.raises(RuntimeError, match=re.escape(str(bad_data))):
        main.ensure_runtime_paths()


def test_scan_series_files_rejects_symlinked_media(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    media = outside / "Show - S01E01.mkv"
    media.write_bytes(b"")
    library = tmp_path / "library"
    library.mkdir()
    link = library / "Show - S01E01.mkv"
    link.symlink_to(media)
    scanned = main.scan_series_files(library)
    assert scanned == {}
    assert not main.path_is_within(library, link)


def test_release_search_persists_candidates_for_selected_anime(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        conn.commit()
    release = {
        "source": "Nyaa", "source_id": "release-1", "title": "Anime - 01",
        "page_url": "https://nyaa.si/view/1", "torrent_url": "magnet:?xt=urn:btih:" + "e" * 40,
        "info_hash": "e" * 40, "score": 10, "score_reason": "1080p",
    }
    monkeypatch.setattr(main, "parse_nyaa", lambda show: [release])
    monkeypatch.setattr(main, "parse_tsukihime", lambda show: [])
    results = main.persist_release_search(anime_id, "Anime")
    assert results[0]["anime_id"] == anime_id
    with main.db() as conn:
        assert conn.execute("SELECT count(*) FROM releases").fetchone()[0] == 1
    main.persist_release_search(anime_id, "Anime")
    with main.db() as conn:
        assert conn.execute("SELECT count(*) FROM releases").fetchone()[0] == 1
