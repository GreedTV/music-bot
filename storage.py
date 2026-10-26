"""SQLite storage for per-server settings and users' saved playlists (data/musicbot.db)."""
import datetime
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

-- listening stats (for /profile, /top and /history)
CREATE TABLE IF NOT EXISTS tracking_optout (
    user_id     INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS listen_daily (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    day         TEXT    NOT NULL,  -- YYYY-MM-DD (UTC)
    seconds     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, day)
);
CREATE TABLE IF NOT EXISTS user_songs (
    user_id     INTEGER NOT NULL,
    song_key    TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    artist      TEXT,
    url         TEXT,
    plays       INTEGER NOT NULL DEFAULT 0,
    seconds     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, song_key)
);
CREATE TABLE IF NOT EXISTS listen_pairs (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    other_id    INTEGER NOT NULL,
    seconds     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, other_id)
);
CREATE TABLE IF NOT EXISTS song_plays (
    id           INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    song_key     TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    url          TEXT,
    artist       TEXT,
    requester_id INTEGER,
    played_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_style (
    user_id     INTEGER PRIMARY KEY,
    color       INTEGER,
    bio         TEXT,
    emoji       TEXT,
    fav_title   TEXT,
    fav_url     TEXT
);
CREATE INDEX IF NOT EXISTS song_plays_by_guild ON song_plays (guild_id, played_at);
CREATE INDEX IF NOT EXISTS song_plays_by_requester ON song_plays (requester_id);
"""

SONG_PLAYS_KEEP_DAYS = 60
HEARD_AFTER_SECONDS = 30  # a song counts as "heard" once someone listened this long


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
    """Delete everything stored about a user (playlists and listening stats).
    Returns how many playlists were removed. Their tracking opt-out, if any, is kept."""
    with _connect() as conn:
        count = conn.execute("DELETE FROM playlists WHERE user_id = ?", (user_id,)).rowcount
        conn.execute("DELETE FROM profile_style WHERE user_id = ?", (user_id,))
        _delete_stats(conn, user_id)
    return count


# ---- profile decorations ----

@dataclass
class ProfileStyle:
    color: int | None = None
    bio: str | None = None
    emoji: str | None = None
    fav_title: str | None = None
    fav_url: str | None = None


_STYLE_FIELDS = ("color", "bio", "emoji", "fav_title", "fav_url")


def get_style(user_id: int) -> ProfileStyle:
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(_STYLE_FIELDS)} FROM profile_style WHERE user_id = ?",
                           (user_id,)).fetchone()
    return ProfileStyle(*row) if row else ProfileStyle()


def update_style(user_id: int, **changes):
    """Set some decoration fields (None clears one)."""
    assert set(changes) <= set(_STYLE_FIELDS)
    style = get_style(user_id)
    for key, value in changes.items():
        setattr(style, key, value)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO profile_style (user_id, {', '.join(_STYLE_FIELDS)}) VALUES (?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT (user_id) DO UPDATE SET " + ", ".join(f"{f} = excluded.{f}" for f in _STYLE_FIELDS),
            (user_id, *(getattr(style, f) for f in _STYLE_FIELDS)))


def reset_style(user_id: int):
    with _connect() as conn:
        conn.execute("DELETE FROM profile_style WHERE user_id = ?", (user_id,))


def _delete_stats(conn: sqlite3.Connection, user_id: int):
    conn.execute("DELETE FROM listen_daily WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_songs WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM listen_pairs WHERE user_id = ? OR other_id = ?", (user_id, user_id))
    conn.execute("UPDATE song_plays SET requester_id = NULL WHERE requester_id = ?", (user_id,))


# ---- listening stats ----

@dataclass
class SongInfo:
    key: str  # YouTube id, or the link for other sites
    title: str
    artist: str | None
    url: str | None


@dataclass
class Profile:
    total_seconds: float
    songs_heard: int
    songs_requested: int
    top_songs: list[tuple[str, str | None, int]]  # (title, url, plays)
    top_artists: list[tuple[str, int]]  # (artist, plays)
    listened_with: list[tuple[int, float]]  # (user id, seconds) in the asked-about server
    top_servers: list[tuple[int, float]]  # (guild id, seconds)
    streak: int
    best_streak: int


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def is_tracking(user_id: int) -> bool:
    with _connect() as conn:
        return not conn.execute("SELECT 1 FROM tracking_optout WHERE user_id = ?", (user_id,)).fetchone()


def set_tracking(user_id: int, enabled: bool):
    """Turning tracking off also deletes the stats collected so far."""
    with _connect() as conn:
        if enabled:
            conn.execute("DELETE FROM tracking_optout WHERE user_id = ?", (user_id,))
        else:
            conn.execute("INSERT OR IGNORE INTO tracking_optout (user_id) VALUES (?)", (user_id,))
            _delete_stats(conn, user_id)


def _opted_out(conn: sqlite3.Connection, user_ids: list[int]) -> set[int]:
    if not user_ids:
        return set()
    marks = ",".join("?" * len(user_ids))
    return {r[0] for r in conn.execute(f"SELECT user_id FROM tracking_optout WHERE user_id IN ({marks})", user_ids)}


def record_play(guild_id: int, song: SongInfo, requester_id: int | None):
    """A song started playing in a server (for /history, /top and 'songs requested')."""
    with _connect() as conn:
        if requester_id is not None and _opted_out(conn, [requester_id]):
            requester_id = None
        conn.execute(
            "INSERT INTO song_plays (guild_id, song_key, title, url, artist, requester_id, played_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, song.key, song.title, song.url, song.artist, requester_id, int(time.time())))


def record_listening(guild_id: int, song: SongInfo, user_ids: list[int], seconds: float, newly_heard: set[int]):
    """These people listened to `song` together for `seconds`."""
    if not user_ids or seconds <= 0:
        return
    day = _today()
    with _connect() as conn:
        skip = _opted_out(conn, user_ids)
        users = [u for u in user_ids if u not in skip]
        if not users:
            return
        conn.executemany(
            "INSERT INTO listen_daily (user_id, guild_id, day, seconds) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, guild_id, day) DO UPDATE SET seconds = seconds + excluded.seconds",
            [(u, guild_id, day, seconds) for u in users])
        conn.executemany(
            "INSERT INTO user_songs (user_id, song_key, title, artist, url, plays, seconds) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, song_key) DO UPDATE SET plays = plays + excluded.plays, "
            "seconds = seconds + excluded.seconds, title = excluded.title",
            [(u, song.key, song.title, song.artist, song.url, int(u in newly_heard), seconds) for u in users])
        conn.executemany(
            "INSERT INTO listen_pairs (guild_id, user_id, other_id, seconds) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id, other_id) DO UPDATE SET seconds = seconds + excluded.seconds",
            [(guild_id, u, o, seconds) for u in users for o in users if u != o])


def _streaks(days: list[str]) -> tuple[int, int]:
    """(current streak, best streak) from listening days."""
    if not days:
        return 0, 0
    dates = sorted({datetime.date.fromisoformat(d) for d in days})
    best = run = 1
    for prev, cur in zip(dates, dates[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        best = max(best, run)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    current = 0
    if (today - dates[-1]).days <= 1:  # listened today or yesterday: the streak is still alive
        current = 1
        for prev, cur in zip(reversed(dates[:-1]), reversed(dates)):
            if (cur - prev).days != 1:
                break
            current += 1
    return current, best


def get_profile(user_id: int, guild_id: int) -> Profile:
    with _connect() as conn:
        total = conn.execute("SELECT COALESCE(SUM(seconds), 0) FROM listen_daily WHERE user_id = ?", (user_id,)).fetchone()[0]
        heard = conn.execute("SELECT COALESCE(SUM(plays), 0) FROM user_songs WHERE user_id = ?", (user_id,)).fetchone()[0]
        requested = conn.execute("SELECT COUNT(*) FROM song_plays WHERE requester_id = ?", (user_id,)).fetchone()[0]
        top_songs = conn.execute(
            "SELECT title, url, plays FROM user_songs WHERE user_id = ? AND plays > 0 "
            "ORDER BY plays DESC, seconds DESC LIMIT 5", (user_id,)).fetchall()
        top_artists = conn.execute(
            "SELECT artist, SUM(plays) AS p FROM user_songs WHERE user_id = ? AND artist IS NOT NULL AND plays > 0 "
            "GROUP BY artist COLLATE NOCASE ORDER BY p DESC, SUM(seconds) DESC LIMIT 5", (user_id,)).fetchall()
        listened_with = conn.execute(
            "SELECT other_id, seconds FROM listen_pairs WHERE guild_id = ? AND user_id = ? "
            "ORDER BY seconds DESC LIMIT 5", (guild_id, user_id)).fetchall()
        top_servers = conn.execute(
            "SELECT guild_id, SUM(seconds) AS s FROM listen_daily WHERE user_id = ? GROUP BY guild_id "
            "ORDER BY s DESC LIMIT 3", (user_id,)).fetchall()
        days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM listen_daily WHERE user_id = ?", (user_id,))]
    streak, best = _streaks(days)
    return Profile(total, heard, requested, top_songs, top_artists, listened_with, top_servers, streak, best)


def recent_plays(guild_id: int, limit: int = 20) -> list[tuple[str, str | None, int | None, int]]:
    """(title, url, requester id, played_at), newest first."""
    with _connect() as conn:
        return conn.execute(
            "SELECT title, url, requester_id, played_at FROM song_plays WHERE guild_id = ? "
            "ORDER BY played_at DESC, id DESC LIMIT ?", (guild_id, limit)).fetchall()


def server_top(guild_id: int, days: int = 7) -> tuple[list[tuple[str, str | None, int]], list[tuple[int, float]]]:
    """This server's most played songs and biggest listeners over the last `days` days."""
    since = int(time.time()) - days * 86400
    since_day = time.strftime("%Y-%m-%d", time.gmtime(since))
    with _connect() as conn:
        songs = conn.execute(
            "SELECT title, url, COUNT(*) AS n FROM song_plays WHERE guild_id = ? AND played_at >= ? "
            "GROUP BY song_key ORDER BY n DESC, MAX(played_at) DESC LIMIT 10", (guild_id, since)).fetchall()
        people = conn.execute(
            "SELECT user_id, SUM(seconds) AS s FROM listen_daily WHERE guild_id = ? AND day >= ? "
            "GROUP BY user_id ORDER BY s DESC LIMIT 10", (guild_id, since_day)).fetchall()
    return songs, people


def prune_old_plays():
    with _connect() as conn:
        conn.execute("DELETE FROM song_plays WHERE played_at < ?", (int(time.time()) - SONG_PLAYS_KEEP_DAYS * 86400,))
