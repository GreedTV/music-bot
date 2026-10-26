"""SQLite storage for per-server settings and users' saved playlists (data/musicbot.db)."""
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "musicbot.db"

MAX_PLAYLISTS_PER_USER = 25
MAX_TRACKS_PER_PLAYLIST = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id          INTEGER PRIMARY KEY,
    volume            REAL    NOT NULL DEFAULT 0.5,
    autoplay          INTEGER NOT NULL DEFAULT 1,
    dj_role_id        INTEGER,
    stay_247          INTEGER NOT NULL DEFAULT 0,
    voice_channel_id  INTEGER,
    text_channel_id   INTEGER
);
CREATE TABLE IF NOT EXISTS playlists (
    user_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL COLLATE NOCASE,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    user_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL COLLATE NOCASE,
    position    INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    youtube_id  TEXT,
    duration    REAL,
    FOREIGN KEY (user_id, name) REFERENCES playlists (user_id, name) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS playlist_tracks_by_list ON playlist_tracks (user_id, name, position);
"""


@dataclass
class GuildSettings:
    guild_id: int
    volume: float = 0.5
    autoplay: bool = True
    dj_role_id: int | None = None
    stay_247: bool = False
    voice_channel_id: int | None = None
    text_channel_id: int | None = None


@dataclass
class SavedTrack:
    title: str
    url: str
    youtube_id: str | None
    duration: float | None


@contextmanager
def _connect():
    """A connection that commits on success, rolls back on error, and always closes."""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    with _connect() as conn:
        conn.executescript(_SCHEMA)


# ---- server settings ----

def load_settings(guild_id: int) -> GuildSettings:
    with _connect() as conn:
        row = conn.execute(
            "SELECT volume, autoplay, dj_role_id, stay_247, voice_channel_id, text_channel_id "
            "FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    if not row:
        return GuildSettings(guild_id)
    return GuildSettings(guild_id, row[0], bool(row[1]), row[2], bool(row[3]), row[4], row[5])


def save_settings(s: GuildSettings):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, volume, autoplay, dj_role_id, stay_247, voice_channel_id, text_channel_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET volume = excluded.volume, "
            "autoplay = excluded.autoplay, dj_role_id = excluded.dj_role_id, stay_247 = excluded.stay_247, "
            "voice_channel_id = excluded.voice_channel_id, text_channel_id = excluded.text_channel_id",
            (s.guild_id, s.volume, int(s.autoplay), s.dj_role_id, int(s.stay_247), s.voice_channel_id, s.text_channel_id))


def servers_with_247() -> list[GuildSettings]:
    with _connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT guild_id FROM guild_settings WHERE stay_247 = 1")]
    return [load_settings(i) for i in ids]


def delete_settings(guild_id: int):
    with _connect() as conn:
        conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))


# ---- saved playlists ----

def list_playlists(user_id: int) -> list[tuple[str, int]]:
    """(name, song count) for each of the user's playlists."""
    with _connect() as conn:
        return conn.execute(
            "SELECT p.name, COUNT(t.position) FROM playlists p LEFT JOIN playlist_tracks t "
            "ON t.user_id = p.user_id AND t.name = p.name WHERE p.user_id = ? GROUP BY p.name ORDER BY p.name",
            (user_id,)).fetchall()


def get_playlist(user_id: int, name: str) -> list[SavedTrack] | None:
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM playlists WHERE user_id = ? AND name = ?", (user_id, name)).fetchone():
            return None
        rows = conn.execute(
            "SELECT title, url, youtube_id, duration FROM playlist_tracks WHERE user_id = ? AND name = ? "
            "ORDER BY position", (user_id, name)).fetchall()
    return [SavedTrack(*r) for r in rows]


def save_playlist(user_id: int, name: str, tracks: list[SavedTrack]) -> str | None:
    """Create or overwrite a playlist. Returns an error message, or None on success."""
    tracks = tracks[:MAX_TRACKS_PER_PLAYLIST]
    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM playlists WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
        if not exists:
            count = conn.execute("SELECT COUNT(*) FROM playlists WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= MAX_PLAYLISTS_PER_USER:
                return f"You already have {MAX_PLAYLISTS_PER_USER} playlists. Delete one first."
            conn.execute("INSERT INTO playlists (user_id, name, created_at) VALUES (?, ?, ?)",
                         (user_id, name, int(time.time())))
        conn.execute("DELETE FROM playlist_tracks WHERE user_id = ? AND name = ?", (user_id, name))
        conn.executemany(
            "INSERT INTO playlist_tracks (user_id, name, position, title, url, youtube_id, duration) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(user_id, name, i, t.title, t.url, t.youtube_id, t.duration) for i, t in enumerate(tracks)])
    return None


def add_to_playlist(user_id: int, name: str, track: SavedTrack) -> str | None:
    existing = get_playlist(user_id, name)
    if existing is None:
        return save_playlist(user_id, name, [track])
    if len(existing) >= MAX_TRACKS_PER_PLAYLIST:
        return f"That playlist is full ({MAX_TRACKS_PER_PLAYLIST} songs)."
    with _connect() as conn:
        conn.execute(
            "INSERT INTO playlist_tracks (user_id, name, position, title, url, youtube_id, duration) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, len(existing), track.title, track.url, track.youtube_id, track.duration))
    return None


def delete_playlist(user_id: int, name: str) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM playlists WHERE user_id = ? AND name = ?", (user_id, name)).rowcount > 0


def delete_user_data(user_id: int) -> int:
    """Delete everything stored about a user. Returns how many playlists were removed."""
    with _connect() as conn:
        return conn.execute("DELETE FROM playlists WHERE user_id = ?", (user_id,)).rowcount
