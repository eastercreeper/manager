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
        conn.execute("INSERT INTO downloads(release_id,state,save_path) VALUES(?,?,?)",
                     (release_id, "completed", str(download_root)))
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
    (source_dir / "Anime - S01E01.mkv").write_bytes(b"video")
    with main.db() as conn:
        anime_id = conn.execute(
            "INSERT INTO anime(title,folder_name,search_query,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Anime", "Anime", "Anime", main.now(), main.now()),
        ).lastrowid
        release_id = conn.execute(
            "INSERT INTO releases(anime_id,source,source_id,title,page_url,created_at) VALUES(?,?,?,?,?,?)",
            (anime_id, "Nyaa", "release", "Anime", "https://example.test", main.now()),
        ).lastrowid
        conn.execute("INSERT INTO downloads(release_id,state,save_path) VALUES(?,?,?)",
                     (release_id, "completed", str(source_dir)))
        conn.commit()
    monkeypatch.setattr(main, "inspect_media", lambda path: {
        "audio": [], "subtitles": [{"codec_name": "subrip", "tags": {"language": "eng"}}],
        "english_ass": False, "japanese_ass": False})
    assert main.import_completed() == 0
    with main.db() as conn:
        row = conn.execute("SELECT state,error FROM downloads").fetchone()
    assert row["state"] == "needs_review"
    assert "ASS/SSA" in row["error"]
