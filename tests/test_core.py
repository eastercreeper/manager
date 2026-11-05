import sqlite3
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
