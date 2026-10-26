"""Discord music bot: YouTube (and other sites) by link or search, with a queue, autoplay, player buttons,
vote-skip and a DJ role, lyrics, audio effects, saved playlists and 24/7 mode."""
import asyncio
import ctypes
import hashlib
import io
import json
import logging
import logging.handlers
import math
import os
import random
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Literal

import aiohttp
import discord
import imageio_ffmpeg
import yt_dlp
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import card
import storage

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")

# Log to bot.log (rotated at 1 MB) so the bot can run in the background with no window.
log_handler = logging.handlers.RotatingFileHandler(
    BASE_DIR / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
log = logging.getLogger("musicbot")

REPO_URL = "https://github.com/GreedTV/music-bot"
TERMS_URL = f"{REPO_URL}/blob/main/TERMS.md"
PRIVACY_URL = f"{REPO_URL}/blob/main/PRIVACY.md"

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

# effect name: (ffmpeg audio filter, playback speed, label shown to users)
FILTERS = {
    "off": ("", 1.0, "Off"),
    "bassboost": ("bass=g=8,alimiter=limit=0.95", 1.0, "Bass boost"),
    "nightcore": ("aresample=48000,asetrate=60000,aresample=48000", 1.25, "Nightcore"),
    "slowed": ("aresample=48000,asetrate=40800,aresample=48000,aecho=0.8:0.88:60:0.35", 0.85, "Slowed + reverb"),
    "8d": ("apulsator=hz=0.08", 1.0, "8D"),
}

YDL_BASE = {"quiet": True, "no_warnings": True}
# Used for /play: single videos are fully resolved, playlists and searches are listed (resolved later).
YDL_ADD = {
    **YDL_BASE,
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "extract_flat": "in_playlist",
    "playlistend": 100,
}
YDL_STREAM = {**YDL_BASE, "format": "bestaudio/best", "noplaylist": True}
YDL_MIX = {**YDL_BASE, "extract_flat": "in_playlist", "playlistend": 25}
YDL_SUGGEST = {**YDL_BASE, "extract_flat": True}

STREAM_MAX_AGE = 60 * 60  # YouTube stream URLs expire after a few hours; re-resolve older ones
MAX_QUEUE = 500
AUTOPLAY = "Autoplay"


class Track:
    def __init__(self, title: str, url: str, requester: str, requester_id: int | None = None,
                 youtube_id: str | None = None, stream_url: str | None = None, duration: float | None = None,
                 uploader: str | None = None):
        self.title = title
        self.uploader = uploader  # YouTube channel name, used to guess the artist
        self.url = url  # page URL, used to (re)resolve the audio stream
        self.requester = requester
        self.requester_id = requester_id
        self.youtube_id = youtube_id
        self.stream_url = stream_url
        self.resolved_at = time.time() if stream_url else 0.0
        self.duration = duration

    @property
    def thumbnail(self) -> str | None:
        return f"https://i.ytimg.com/vi/{self.youtube_id}/hqdefault.jpg" if self.youtube_id else None

    def to_saved(self) -> storage.SavedTrack:
        return storage.SavedTrack(self.title, self.url, self.youtube_id, self.duration)

    @property
    def artist(self) -> str | None:
        """'Daft Punk - Get Lucky' -> 'Daft Punk'; otherwise the channel name without ' - Topic' / 'VEVO'."""
        cleaned = clean_title(self.title)
        if " - " in cleaned:
            return cleaned.split(" - ", 1)[0].strip() or None
        if self.uploader:
            name = re.sub(r"\s*-\s*Topic$|VEVO$|\s+Official$", "", self.uploader, flags=re.I).strip()
            return name or None
        return None

    def song_info(self) -> storage.SongInfo:
        return storage.SongInfo(self.youtube_id or self.url, self.title, self.artist, self.url)


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "Live"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def parse_time(text: str) -> float | None:
    """'90', '1:30' or '1:02:03' -> seconds."""
    try:
        parts = [float(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


class GuildPlayer:
    """Per-server queue and playback state. Volume, autoplay, DJ role and 24/7 are saved in the database."""

    def __init__(self, guild_id: int):
        self.settings = storage.load_settings(guild_id)
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop: Literal["off", "song", "queue"] = "off"
        self.filter = "off"
        self.history: deque[str] = deque(maxlen=100)  # recently played YouTube ids, so autoplay avoids repeats
        self.previous: deque[Track] = deque(maxlen=25)  # for /previous
        self.text_channel: discord.abc.Messageable | None = None
        self.np_message: discord.Message | None = None  # the "Now playing" card with the buttons
        self.skipping = False
        self.going_back = False
        self.seek_to: float | None = None  # set to restart the current song at a position (seek or new effect)
        self.skip_votes: set[int] = set()
        self.elapsed_base = 0.0
        self.resumed_at: float | None = None  # None while paused
        self.lock = asyncio.Lock()
        self.listened: dict[int, float] = {}  # seconds each person has heard of the current song (for stats)
        self.last_stats_tick: float | None = None

    @property
    def volume(self) -> float:
        return self.settings.volume

    @volume.setter
    def volume(self, value: float):
        self.settings.volume = value
        storage.save_settings(self.settings)

    @property
    def autoplay(self) -> bool:
        return self.settings.autoplay

    @autoplay.setter
    def autoplay(self, value: bool):
        self.settings.autoplay = value
        storage.save_settings(self.settings)

    @property
    def paused(self) -> bool:
        return self.current is not None and self.resumed_at is None

    def elapsed(self) -> float:
        """Position in the song, in song time (effects like nightcore play faster than real time)."""
        if self.resumed_at is None:
            return self.elapsed_base
        return self.elapsed_base + (time.monotonic() - self.resumed_at) * FILTERS[self.filter][1]

    def mark_playing(self, position: float):
        self.elapsed_base, self.resumed_at = position, time.monotonic()

    def mark_paused(self):
        self.elapsed_base, self.resumed_at = self.elapsed(), None


class MusicTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "I need to be added to this server to play music. Use the **Add App** button on my profile.",
                ephemeral=True)
            return False
        return True


intents = discord.Intents.default()
intents.message_content = True  # for M. prefix commands; must also be switched on in the Developer Portal
client = discord.Client(intents=intents, activity=discord.CustomActivity(name="Type /help or M.help"))
tree = MusicTree(client)
players: dict[int, GuildPlayer] = {}
http_session: aiohttp.ClientSession | None = None


def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer(guild_id)
    return players[guild_id]


# ---- finding songs ----

def _youtube_id(info: dict) -> str | None:
    key = (info.get("extractor_key") or info.get("ie_key") or "").lower()
    return info.get("id") if key.startswith("youtube") else None


def _entry_to_track(entry: dict, requester: str, requester_id: int | None) -> Track:
    url = entry.get("webpage_url") or entry.get("url")
    yt_id = _youtube_id(entry)
    if yt_id and not str(url).startswith("http"):
        url = f"https://www.youtube.com/watch?v={yt_id}"
    return Track(entry.get("title") or url, url, requester, requester_id, yt_id, duration=entry.get("duration"),
                 uploader=entry.get("channel") or entry.get("uploader"))


async def fetch_tracks(query: str, requester: str, requester_id: int | None = None) -> tuple[list[Track], str | None]:
    """Return the tracks for a link or search, plus the playlist name if it was a playlist."""
    def _extract():
        with yt_dlp.YoutubeDL(YDL_ADD) as ydl:
            return ydl.extract_info(query, download=False)

    info = await asyncio.to_thread(_extract)
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise ValueError("no results")
        if (info.get("extractor_key") or "").lower().startswith("youtubesearch"):
            return [_entry_to_track(entries[0], requester, requester_id)], None
        return [_entry_to_track(e, requester, requester_id) for e in entries], info.get("title") or "playlist"
    track = Track(info["title"], info.get("webpage_url", query), requester, requester_id, _youtube_id(info),
                  info.get("url"), info.get("duration"), info.get("artist") or info.get("channel") or info.get("uploader"))
    return [track], None


async def resolve_stream(track: Track) -> str:
    if track.stream_url and time.time() - track.resolved_at < STREAM_MAX_AGE:
        return track.stream_url

    def _extract():
        with yt_dlp.YoutubeDL(YDL_STREAM) as ydl:
            return ydl.extract_info(track.url, download=False)

    info = await asyncio.to_thread(_extract)
    track.stream_url, track.resolved_at = info["url"], time.time()
    track.youtube_id = track.youtube_id or _youtube_id(info)
    track.duration = track.duration or info.get("duration")
    track.uploader = track.uploader or info.get("artist") or info.get("channel") or info.get("uploader")
    return track.stream_url


async def find_related(seed: Track, player: GuildPlayer) -> Track | None:
    """Pick a song from YouTube's Mix for the seed track (similar songs and similar artists)."""
    seed_id = seed.youtube_id
    if not seed_id:  # e.g. a SoundCloud track: find the same song on YouTube first
        found, _ = await fetch_tracks(f"ytsearch1:{seed.title}", AUTOPLAY)
        seed_id = found[0].youtube_id
    if not seed_id:
        return None

    def _extract():
        with yt_dlp.YoutubeDL(YDL_MIX) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}", download=False)

    info = await asyncio.to_thread(_extract)
    queued = {t.youtube_id for t in player.queue}
    fresh = [e for e in info.get("entries") or []
             if e and e.get("id") and e["id"] not in player.history and e["id"] not in queued and e["id"] != seed_id]
    if not fresh:
        return None
    # Choose among the top few so it varies between similar songs and similar artists.
    return _entry_to_track(random.choice(fresh[:5]), AUTOPLAY, None)


# ---- who can do what ----

def listeners(vc: discord.VoiceClient) -> int:
    return sum(1 for m in vc.channel.members if not m.bot)


def listener_ids(vc: discord.VoiceClient) -> set[int]:
    return {m.id for m in vc.channel.members if not m.bot}


def is_active(vc: discord.VoiceClient | None) -> bool:
    return bool(vc and (vc.is_playing() or vc.is_paused()))


def is_dj(member: discord.Member, player: GuildPlayer) -> bool:
    """Server managers, people with the DJ role, and anyone alone with the bot."""
    if member.guild_permissions.manage_guild:
        return True
    role_id = player.settings.dj_role_id
    if role_id and any(r.id == role_id for r in member.roles):
        return True
    vc = member.guild.voice_client
    return bool(vc and member in vc.channel.members and listeners(vc) <= 1)


def can_control(member: discord.Member, player: GuildPlayer) -> bool:
    """Without a DJ role everyone can control playback; with one, only DJs can."""
    return not player.settings.dj_role_id or is_dj(member, player)


async def reply(interaction: discord.Interaction, text: str, ephemeral: bool = True):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral)


async def guard(interaction: discord.Interaction, *, playing: bool = False, listener: bool = True,
                dj: bool = False) -> GuildPlayer | None:
    """Common checks for commands and buttons. Replies with the reason and returns None if not allowed."""
    guild = interaction.guild
    vc = guild.voice_client
    player = players.get(guild.id)
    if playing and (not vc or not player or not player.current):
        await reply(interaction, "Nothing is playing.")
        return None
    if listener and (not vc or not interaction.user.voice or interaction.user.voice.channel != vc.channel):
        await reply(interaction, "Join my voice channel first." if vc else "I'm not in a voice channel.")
        return None
    player = player or get_player(guild.id)
    if dj and not can_control(interaction.user, player):
        await reply(interaction, f"Only DJs can do that: server managers and people with the "
                                 f"<@&{player.settings.dj_role_id}> role.")
        return None
    return player


# ---- playback ----

def make_source(stream: str, position: float, player: GuildPlayer) -> discord.AudioSource:
    before = FFMPEG_RECONNECT + (f" -ss {position:.2f}" if position > 0 else "")
    effect = FILTERS[player.filter][0]
    options = "-vn" + (f" -af {effect}" if effect else "")
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(stream, executable=FFMPEG_EXE, before_options=before, options=options),
        volume=player.volume)


def _after_playback(guild: discord.Guild, err: Exception | None):
    if err:
        log.error("Playback error: %s", err)
    asyncio.run_coroutine_threadsafe(play_next(guild), client.loop)


async def start_track(guild: discord.Guild, player: GuildPlayer, track: Track, position: float) -> bool:
    try:
        stream = await resolve_stream(track)
    except Exception as e:
        log.warning("Could not play %s: %s", track.url, e)
        return False
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return False
    player.current = track
    if position == 0:
        if track.youtube_id:
            player.history.append(track.youtube_id)
        player.listened = {}
        try:
            storage.record_play(guild.id, track.song_info(), track.requester_id)
        except Exception:
            log.exception("Could not record play")
    vc.play(make_source(stream, position, player), after=lambda err: _after_playback(guild, err))
    player.mark_playing(position)
    return True


async def announce(player: GuildPlayer, text: str):
    if player.text_channel:
        try:
            await player.text_channel.send(text)
        except discord.HTTPException:
            log.warning("Could not send message to text channel")


async def play_next(guild: discord.Guild):
    """Start the next track: seek / loop / queue / autoplay, in that order."""
    player = get_player(guild.id)
    async with player.lock:
        vc = guild.voice_client
        if not vc or not vc.is_connected() or vc.is_playing() or vc.is_paused():
            return

        finished = player.current
        if player.seek_to is not None and finished:
            position, player.seek_to = player.seek_to, None
            if await start_track(guild, player, finished, position):
                await refresh_now_playing(guild)
                return
        player.seek_to = None
        player.current = None

        if finished and not player.going_back:
            if player.loop == "song" and not player.skipping:
                player.queue.appendleft(finished)
            else:
                player.previous.append(finished)
                if player.loop == "queue":
                    player.queue.append(finished)
        player.skipping = player.going_back = False
        player.skip_votes.clear()
        await retire_now_playing(player)

        for _attempt in range(5):  # give up after a few broken songs in a row (e.g. network down)
            if player.queue:
                track = player.queue.popleft()
            elif player.autoplay and finished and listeners(vc) > 0:
                try:
                    track = await find_related(finished, player)
                except Exception:
                    log.exception("Autoplay lookup failed")
                    track = None
                if not track:
                    await announce(player, "Autoplay couldn't find a related song. Queue finished.")
                    return
            else:
                return

            if await start_track(guild, player, track, 0.0):
                await post_now_playing(player)
                return
            await announce(player, f"⚠️ Couldn't play **{track.title}**, skipping.")
            finished = track
        await announce(player, "⚠️ Several songs in a row failed to load. Stopping for now.")


def toggle_pause(guild: discord.Guild, player: GuildPlayer):
    vc = guild.voice_client
    if vc.is_paused():
        vc.resume()
        player.mark_playing(player.elapsed())
    elif vc.is_playing():
        vc.pause()
        player.mark_paused()


def restart_at(guild: discord.Guild, player: GuildPlayer, position: float):
    """Replay the current song from a position (used by /seek and audio effects)."""
    player.seek_to = position
    guild.voice_client.stop()  # play_next sees seek_to and restarts the same song


def do_skip(member: discord.Member, player: GuildPlayer) -> str:
    """Skip right away for DJs and the person who asked for the song; otherwise count a vote."""
    vc = member.guild.voice_client
    track = player.current
    if is_dj(member, player) or (track.requester_id is not None and track.requester_id == member.id):
        player.skipping = True
        vc.stop()
        return f"⏭️ **{member.display_name}** skipped **{track.title}**."
    voters = listener_ids(vc)
    player.skip_votes = {v for v in player.skip_votes if v in voters} | {member.id}
    needed = math.ceil(len(voters) / 2)
    if len(player.skip_votes) >= needed:
        player.skipping = True
        vc.stop()
        return f"⏭️ Vote passed ({len(player.skip_votes)}/{needed}). Skipped **{track.title}**."
    return (f"🗳️ **{member.display_name}** voted to skip ({len(player.skip_votes)}/{needed}). "
            f"{needed - len(player.skip_votes)} more vote(s) needed.")


async def go_back(guild: discord.Guild, player: GuildPlayer):
    prev = player.previous.pop()
    if player.current:
        player.queue.appendleft(player.current)
    player.queue.appendleft(prev)
    vc = guild.voice_client
    if is_active(vc):
        player.going_back = True
        vc.stop()
    else:
        await play_next(guild)


async def stop_playback(guild: discord.Guild) -> bool:
    """Stop the music. Returns True if the bot stayed in the channel because 24/7 mode is on."""
    player = players.get(guild.id)
    vc = guild.voice_client
    if player and player.settings.stay_247 and vc and vc.is_connected():
        player.queue.clear()
        player.loop = "off"
        player.current = None
        await retire_now_playing(player)
        vc.stop()
        return True
    players.pop(guild.id, None)
    if player:
        await retire_now_playing(player)
    if vc:
        await vc.disconnect()
    return False


def enqueue(player: GuildPlayer, tracks: list[Track]) -> int:
    tracks = tracks[:MAX_QUEUE - len(player.queue)]
    player.queue.extend(tracks)
    return len(tracks)


async def join_caller(interaction: discord.Interaction) -> discord.VoiceClient | None:
    """Connect to the caller's voice channel (call after defer). Replies and returns None if it can't."""
    guild = interaction.guild
    voice = interaction.user.voice
    if not voice or not voice.channel:
        await reply(interaction, "Join a voice channel first.")
        return None
    channel = voice.channel
    player = get_player(guild.id)
    vc = guild.voice_client
    if vc and vc.channel != channel:
        if is_active(vc) and listeners(vc) > 0 and not is_dj(interaction.user, player):
            await reply(interaction, f"I'm already playing music in {vc.channel.mention}. Join that channel instead.")
            return None
        await vc.move_to(channel)
    elif not vc:
        perms = channel.permissions_for(guild.me)
        if not perms.connect or not perms.speak:
            await reply(interaction, f"I don't have permission to join and speak in {channel.mention}.")
            return None
        try:
            vc = await channel.connect(timeout=15, self_deaf=True)
        except Exception:
            log.exception("Could not join voice channel")
            await reply(interaction, "I couldn't join your voice channel. Please try again.")
            return None
    player.text_channel = interaction.channel
    if player.settings.stay_247:
        player.settings.voice_channel_id, player.settings.text_channel_id = channel.id, interaction.channel.id
        storage.save_settings(player.settings)
    return vc


# ---- "Now playing" card with control buttons ----

def progress_bar(elapsed: float, duration: float | None, width: int = 16) -> str:
    if not duration:
        return "🔴 Live"
    elapsed = min(elapsed, duration)
    filled = min(width - 1, int(width * elapsed / duration))
    return "▬" * filled + "🔘" + "▬" * (width - filled - 1) + f"  `{format_duration(elapsed)} / {format_duration(duration)}`"


def now_playing_embed(player: GuildPlayer) -> discord.Embed:
    track = player.current
    embed = discord.Embed(title=track.title[:256], url=track.url if str(track.url).startswith("http") else None,
                          description=progress_bar(player.elapsed(), track.duration),
                          color=0xFAA61A if player.paused else 0x5865F2)
    embed.set_author(name="⏸️ Paused" if player.paused else "🎵 Now playing")
    embed.add_field(name="Requested by",
                    value="🔁 Autoplay (similar song)" if track.requester == AUTOPLAY else track.requester)
    if player.queue:
        up_next = player.queue[0].title
    elif player.loop != "off":
        up_next = "This song again (loop)"
    else:
        up_next = "Autoplay will pick a similar song" if player.autoplay else "Nothing. Add songs with /play"
    embed.add_field(name="Up next", value=up_next[:1024])
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    effect = f" · Effect: {FILTERS[player.filter][2]}" if player.filter != "off" else ""
    effect += " · 24/7" if player.settings.stay_247 else ""
    embed.set_footer(text=f"Loop: {player.loop} · Autoplay: {'on' if player.autoplay else 'off'}{effect} · "
                          f"Volume: {round(player.volume * 100)}% · {len(player.queue)} in queue")
    return embed


class PlayerControls(discord.ui.View):
    """Buttons under the "Now playing" card. Persistent: custom_ids keep them working after a restart."""

    def __init__(self, player: GuildPlayer | None = None):
        super().__init__(timeout=None)
        if player and player.paused:
            self.pause_resume.label, self.pause_resume.emoji = "Resume", "▶️"
        if player and player.loop != "off":
            self.loop_mode.label = f"Loop: {player.loop}"
            self.loop_mode.style = discord.ButtonStyle.success
        if player and player.autoplay:
            self.autoplay_mode.label = "Autoplay: on"
            self.autoplay_mode.style = discord.ButtonStyle.success
        if player and player.settings.stay_247:
            self.stay_mode.label = "24/7: on"
            self.stay_mode.style = discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected() or interaction.guild.id not in players:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction):
        player = get_player(interaction.guild.id)
        if player.current:
            await interaction.response.edit_message(embed=now_playing_embed(player), view=PlayerControls(player))
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Back", emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="musicbot:previous", row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await guard(interaction, dj=True)
        if not player:
            return
        if not player.previous:
            return await interaction.response.send_message("There's no previous song.", ephemeral=True)
        await interaction.response.send_message(
            f"⏮️ **{interaction.user.display_name}** went back to **{player.previous[-1].title}**.")
        await go_back(interaction.guild, player)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="musicbot:pause", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await guard(interaction, playing=True, dj=True)
        if player:
            toggle_pause(interaction.guild, player)
            await self._redraw(interaction)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="musicbot:skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await guard(interaction, playing=True)
        if player:
            await interaction.response.send_message(do_skip(interaction.user, player))

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="musicbot:stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await guard(interaction, dj=True):
            await interaction.response.send_message(f"⏹️ **{interaction.user.display_name}** stopped the music.")
            await stop_playback(interaction.guild)

    @discord.ui.button(label="Loop: off", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="musicbot:loop", row=1)
    async def loop_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await guard(interaction, playing=True, dj=True)
        if player:
            player.loop = {"off": "song", "song": "queue", "queue": "off"}[player.loop]
            await self._redraw(interaction)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="musicbot:shuffle", row=1)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await guard(interaction, dj=True)
        if not player:
            return
        if not shuffle_queue(player):
            return await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
        await self._redraw(interaction)
        await interaction.followup.send(f"🔀 **{interaction.user.display_name}** shuffled the queue.")

    @discord.ui.button(label="Autoplay: off", emoji="📻", style=discord.ButtonStyle.secondary,
                       custom_id="musicbot:autoplay", row=1)
    async def autoplay_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await guard(interaction, dj=True)
        if player:
            player.autoplay = not player.autoplay
            await self._redraw(interaction)

    @discord.ui.button(label="24/7: off", emoji="♾️", style=discord.ButtonStyle.secondary, custom_id="musicbot:247", row=2)
    async def stay_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await guard(interaction)
        if not player:
            return
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Only server managers can turn 24/7 mode on or off.",
                                                           ephemeral=True)
        on = not player.settings.stay_247
        set_247(player, on, interaction.guild.voice_client.channel, interaction.channel)
        await self._redraw(interaction)
        await interaction.followup.send(f"♾️ **{interaction.user.display_name}** turned 24/7 mode **{'on' if on else 'off'}**."
                                        + (" I'll stay in this voice channel, even after a restart." if on else ""))

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="musicbot:queue", row=1)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(queue_text(get_player(interaction.guild.id)), ephemeral=True)

    @discord.ui.button(label="Lyrics", emoji="🎤", style=discord.ButtonStyle.secondary, custom_id="musicbot:lyrics", row=1)
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild.id)
        if not player.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_lyrics(interaction, clean_title(player.current.title), player.current.duration, ephemeral=True)


async def post_now_playing(player: GuildPlayer):
    if not player.text_channel or not player.current:
        return
    try:
        player.np_message = await player.text_channel.send(embed=now_playing_embed(player), view=PlayerControls(player))
    except discord.HTTPException:
        log.warning("Could not send the now-playing card (missing Send Messages or Embed Links permission?)")


async def refresh_now_playing(guild: discord.Guild):
    """Redraw the card after something it shows changed."""
    player = players.get(guild.id)
    if not player or not player.np_message or not player.current:
        return
    try:
        await player.np_message.edit(embed=now_playing_embed(player), view=PlayerControls(player))
    except discord.HTTPException:
        pass


async def retire_now_playing(player: GuildPlayer):
    """Remove the buttons from the old card so only the current song has controls."""
    message, player.np_message = player.np_message, None
    if message:
        try:
            await message.edit(view=None)
        except discord.HTTPException:
            pass


def shuffle_queue(player: GuildPlayer) -> bool:
    if len(player.queue) < 2:
        return False
    items = list(player.queue)
    random.shuffle(items)
    player.queue = deque(items)
    return True


def queue_text(player: GuildPlayer, page: int = 1) -> str:
    effect = f" · Effect: **{FILTERS[player.filter][2]}**" if player.filter != "off" else ""
    status = f"Loop: **{player.loop}** · Autoplay: **{'on' if player.autoplay else 'off'}**{effect}"
    if not player.current and not player.queue:
        return f"The queue is empty.\n{status}"
    per_page = 10
    pages = max(1, -(-len(player.queue) // per_page))
    page = min(page, pages)
    lines = []
    if player.current:
        lines.append(f"**Now playing:** {player.current.title} (requested by {player.current.requester})")
    start = (page - 1) * per_page
    for i, t in enumerate(list(player.queue)[start:start + per_page], start=start + 1):
        lines.append(f"`{i}.` {t.title} · {format_duration(t.duration)} (requested by {t.requester})")
    if player.queue:
        lines.append(f"Page {page}/{pages} · {len(player.queue)} songs queued")
    lines.append(status)
    return "\n".join(lines)


# ---- lyrics (lrclib.net) ----

async def get_http() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(
            headers={"User-Agent": f"MusicaDiscordBot ({REPO_URL})"}, timeout=aiohttp.ClientTimeout(total=10))
    return http_session


def clean_title(title: str) -> str:
    """'Daft Punk - Get Lucky (Official Audio) ft. Pharrell' -> 'Daft Punk - Get Lucky'."""
    t = re.sub(r"[(\[{][^)\]}]*[)\]}]", " ", title)
    parts = [re.sub(r"\s(ft|feat|featuring)\.?\s.*$", "", p, flags=re.I) for p in t.split(" - ")]
    t = " - ".join(parts)
    t = re.sub(r"\b(official|music|lyrics?|video|audio|hd|4k|remaster(ed)?|visuali[sz]er|mv)\b", " ", t, flags=re.I)
    return re.sub(r"[\s|]+", " ", t).strip(" -")


async def find_lyrics(query: str, duration: float | None = None) -> dict | None:
    session = await get_http()
    searches = [{"q": query}]
    if " - " in query:  # "Artist - Song": an exact artist/title search finds the original over remixes
        artist, _, song = query.partition(" - ")
        searches.insert(0, {"artist_name": artist, "track_name": song})
    for params in searches:
        async with session.get("https://lrclib.net/api/search", params=params) as resp:
            if resp.status != 200:
                continue
            results = [r for r in await resp.json() if r.get("plainLyrics") or r.get("instrumental")]
        if not results:
            continue
        if duration:  # prefer the version with the same length as what's playing
            close = [r for r in results if abs((r.get("duration") or 0) - duration) <= 5]
            if close:
                return close[0]
        return results[0]
    return None


async def send_lyrics(interaction: discord.Interaction, query: str, duration: float | None, ephemeral: bool):
    try:
        result = await find_lyrics(query, duration)
    except Exception:
        log.exception("Lyrics lookup failed")
        result = None
    if not result:
        return await interaction.followup.send(f"No lyrics found for **{query}**.", ephemeral=ephemeral)
    text = "🎶 This song is instrumental." if result.get("instrumental") else result["plainLyrics"].strip()
    if len(text) > 4000:
        text = text[:4000].rsplit("\n", 1)[0] + "\n…"
    embed = discord.Embed(title=f"{result['trackName']} · {result['artistName']}"[:256], description=text, color=0x5865F2)
    embed.set_footer(text="Lyrics from lrclib.net")
    await interaction.followup.send(embed=embed, ephemeral=ephemeral)


# ---- commands: playing ----

_suggest_cache: dict[str, tuple[float, list[app_commands.Choice[str]]]] = {}
_latest_typing: dict[int, str] = {}


def _search_suggestions(text: str) -> list[dict]:
    with yt_dlp.YoutubeDL(YDL_SUGGEST) as ydl:
        return ydl.extract_info(f"ytsearch5:{text}", download=False).get("entries") or []


@tree.command(description="Play a song or playlist from a link (YouTube, SoundCloud, ...) or search terms")
@app_commands.describe(query="Link or search terms. Pick a suggestion, or press Enter to play the top result")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await join_caller(interaction)
    if not vc:
        return
    player = get_player(interaction.guild.id)
    try:
        tracks, playlist = await fetch_tracks(query, interaction.user.display_name, interaction.user.id)
    except Exception as e:
        log.warning("Search failed for %r: %s", query, e)
        return await interaction.followup.send("Couldn't find anything for that.")

    added = enqueue(player, tracks)
    if not added:
        return await interaction.followup.send(f"The queue is full ({MAX_QUEUE} songs).")
    if playlist:
        msg = f"Added **{added}** songs from **{playlist}** to the queue."
    elif is_active(vc):
        msg = f"Queued **{tracks[0].title}** (position {len(player.queue)})"
    else:
        msg = f"Loading **{tracks[0].title}**..."
    await interaction.followup.send(msg)
    if is_active(vc):
        await refresh_now_playing(interaction.guild)
    else:
        await play_next(interaction.guild)


@play.autocomplete("query")
async def play_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """YouTube suggestions while typing. Discord allows 3 seconds, so slow searches are dropped."""
    text = current.strip()
    if len(text) < 3 or text.startswith(("http://", "https://")):
        return []
    key = text.lower()
    hit = _suggest_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    _latest_typing[interaction.user.id] = key
    await asyncio.sleep(0.3)  # wait until they stop typing for a moment
    if _latest_typing.get(interaction.user.id) != key:
        return []
    try:
        entries = await asyncio.wait_for(asyncio.to_thread(_search_suggestions, text), timeout=2.3)
    except Exception:
        return []
    choices = []
    for e in entries:
        if e and e.get("id"):
            name = f"{(e.get('title') or e['id'])[:85]} · {format_duration(e.get('duration'))}"
            choices.append(app_commands.Choice(name=name[:100], value=f"https://www.youtube.com/watch?v={e['id']}"))
    if len(_suggest_cache) > 500:
        _suggest_cache.clear()
    _suggest_cache[key] = (time.time(), choices)
    return choices


@tree.command(description="Skip the current song (others may need to vote)")
async def skip(interaction: discord.Interaction):
    player = await guard(interaction, playing=True)
    if player:
        await interaction.response.send_message(do_skip(interaction.user, player))


@tree.command(description="Go back to the previous song")
async def previous(interaction: discord.Interaction):
    player = await guard(interaction, dj=True)
    if not player:
        return
    if not player.previous:
        return await interaction.response.send_message("There's no previous song.", ephemeral=True)
    await interaction.response.send_message(f"⏮️ Going back to **{player.previous[-1].title}**.")
    await go_back(interaction.guild, player)


@tree.command(description="Jump to a time in the current song")
@app_commands.describe(time="For example 1:30 or 90")
async def seek(interaction: discord.Interaction, time: str):
    player = await guard(interaction, playing=True, dj=True)
    if not player:
        return
    position = parse_time(time)
    duration = player.current.duration
    if not duration:
        return await interaction.response.send_message("You can't seek in a live stream.", ephemeral=True)
    if position is None or position >= duration:
        return await interaction.response.send_message(
            f"Give a time between 0:00 and {format_duration(duration)}, like `1:30`.", ephemeral=True)
    await interaction.response.send_message(f"⏩ Jumping to **{format_duration(position)}**.")
    restart_at(interaction.guild, player, position)


@tree.command(description="Pause playback")
async def pause(interaction: discord.Interaction):
    player = await guard(interaction, playing=True, dj=True)
    if not player:
        return
    if player.paused:
        return await interaction.response.send_message("Already paused. Use /resume.", ephemeral=True)
    toggle_pause(interaction.guild, player)
    await interaction.response.send_message("⏸️ Paused.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    player = await guard(interaction, playing=True, dj=True)
    if not player:
        return
    if not player.paused:
        return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
    toggle_pause(interaction.guild, player)
    await interaction.response.send_message("▶️ Resumed.")
    await refresh_now_playing(interaction.guild)


@tree.command(name="filter", description="Add an audio effect to the music")
@app_commands.describe(effect="The effect to use")
@app_commands.choices(effect=[app_commands.Choice(name=label, value=key) for key, (_, _, label) in FILTERS.items()])
async def audio_filter(interaction: discord.Interaction, effect: app_commands.Choice[str]):
    player = await guard(interaction, dj=True)
    if not player:
        return
    vc = interaction.guild.voice_client
    position = player.elapsed() if player.current and player.current.duration else 0.0
    player.filter = effect.value
    await interaction.response.send_message(f"🎛️ Effect: **{effect.name}**")
    if player.current and is_active(vc):
        restart_at(interaction.guild, player, position)


@tree.command(description="Show the lyrics of the current song, or of any song")
@app_commands.describe(song="Song to look up (leave empty for the current song)")
async def lyrics(interaction: discord.Interaction, song: str | None = None):
    player = players.get(interaction.guild.id)
    if not song and not (player and player.current):
        return await interaction.response.send_message("Nothing is playing. Try `/lyrics song: name`.", ephemeral=True)
    await interaction.response.defer(thinking=True)
    if song:
        await send_lyrics(interaction, song, None, ephemeral=False)
    else:
        await send_lyrics(interaction, clean_title(player.current.title), player.current.duration, ephemeral=False)


@tree.command(description="Stop, clear the queue, and leave the voice channel")
async def stop(interaction: discord.Interaction):
    if not await guard(interaction, dj=True):
        return
    await interaction.response.defer()
    stayed = await stop_playback(interaction.guild)
    await interaction.followup.send("⏹️ Stopped. Staying in the channel because 24/7 mode is on." if stayed
                                    else "⏹️ Stopped and left the channel.")


@tree.command(description="Set volume (0-100)")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
    player = await guard(interaction, dj=True)
    if not player:
        return
    player.volume = percent / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to {percent}%.")
    await refresh_now_playing(interaction.guild)


# ---- commands: queue ----

@tree.command(description="Show the queue")
@app_commands.describe(page="Page number")
async def queue(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
    await interaction.response.send_message(queue_text(get_player(interaction.guild.id), page))


@tree.command(name="nowplaying", description="Show the current song with the player buttons")
async def now_playing(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if not player or not player.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    # Move the card (and its buttons) down to the latest message in this channel.
    await retire_now_playing(player)
    player.text_channel = interaction.channel
    await interaction.response.send_message(embed=now_playing_embed(player), view=PlayerControls(player))
    player.np_message = await interaction.original_response()


@tree.command(description="Remove a song from the queue")
@app_commands.describe(position="Position in /queue")
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    player = get_player(interaction.guild.id)
    if position > len(player.queue):
        return await interaction.response.send_message("There's no song at that position.", ephemeral=True)
    track = player.queue[position - 1]
    if track.requester_id != interaction.user.id and not can_control(interaction.user, player):
        return await interaction.response.send_message("You can only remove songs you added.", ephemeral=True)
    del player.queue[position - 1]
    await interaction.response.send_message(f"Removed **{track.title}**.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Move a song to a different position in the queue")
async def move(interaction: discord.Interaction, from_position: app_commands.Range[int, 1],
               to_position: app_commands.Range[int, 1]):
    player = await guard(interaction, dj=True)
    if not player:
        return
    if from_position > len(player.queue):
        return await interaction.response.send_message("There's no song at that position.", ephemeral=True)
    track = player.queue[from_position - 1]
    del player.queue[from_position - 1]
    player.queue.insert(min(to_position, len(player.queue) + 1) - 1, track)
    await interaction.response.send_message(f"Moved **{track.title}** to position {min(to_position, len(player.queue))}.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Skip straight to a song in the queue")
@app_commands.describe(position="Position in /queue")
async def skipto(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    player = await guard(interaction, dj=True)
    if not player:
        return
    if position > len(player.queue):
        return await interaction.response.send_message("There's no song at that position.", ephemeral=True)
    for _ in range(position - 1):
        dropped = player.queue.popleft()
        if player.loop == "queue":
            player.queue.append(dropped)
    await interaction.response.send_message(f"⏭️ Skipping to **{player.queue[0].title}**.")
    vc = interaction.guild.voice_client
    player.skipping = True
    if is_active(vc):
        vc.stop()
    else:
        await play_next(interaction.guild)


@tree.command(description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    player = await guard(interaction, dj=True)
    if not player:
        return
    if not shuffle_queue(player):
        return await interaction.response.send_message("Not enough songs to shuffle.", ephemeral=True)
    await interaction.response.send_message("🔀 Shuffled the queue.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Clear upcoming songs (keeps the current one playing)")
async def clear(interaction: discord.Interaction):
    player = await guard(interaction, dj=True)
    if not player:
        return
    player.queue.clear()
    await interaction.response.send_message("🧹 Cleared the queue.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Repeat the current song, the whole queue, or nothing")
async def loop(interaction: discord.Interaction, mode: Literal["off", "song", "queue"]):
    player = await guard(interaction, dj=True)
    if not player:
        return
    player.loop = mode
    await interaction.response.send_message(f"🔁 Loop set to **{mode}**.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="When the queue ends, keep playing similar songs")
async def autoplay(interaction: discord.Interaction, mode: Literal["on", "off"]):
    player = await guard(interaction, listener=False, dj=True)
    if not player:
        return
    player.autoplay = mode == "on"
    await interaction.response.send_message(f"Autoplay is now **{mode}**.")
    await refresh_now_playing(interaction.guild)


# ---- commands: saved playlists ----

playlist_group = app_commands.Group(name="playlist", description="Your saved playlists")


async def playlist_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    names = storage.list_playlists(interaction.user.id)
    return [app_commands.Choice(name=f"{name} ({count} songs)"[:100], value=name)
            for name, count in names if current.lower() in name.lower()][:25]


@playlist_group.command(name="save", description="Save the current song and the queue as a playlist")
@app_commands.describe(name="Playlist name (saving with an existing name replaces it)")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_save(interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]):
    player = players.get(interaction.guild.id)
    tracks = ([player.current] if player and player.current else []) + (list(player.queue) if player else [])
    if not tracks:
        return await interaction.response.send_message("Nothing is playing or queued to save.", ephemeral=True)
    replaced = storage.get_playlist(interaction.user.id, name) is not None
    error = storage.save_playlist(interaction.user.id, name, [t.to_saved() for t in tracks])
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    count = min(len(tracks), storage.MAX_TRACKS_PER_PLAYLIST)
    await interaction.response.send_message(
        f"💾 {'Replaced' if replaced else 'Saved'} playlist **{name}** with {count} songs. Play it with `/playlist play`.",
        ephemeral=True)


@playlist_group.command(name="add", description="Add the current song to one of your playlists")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_add(interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]):
    player = players.get(interaction.guild.id)
    if not player or not player.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    error = storage.add_to_playlist(interaction.user.id, name, player.current.to_saved())
    await interaction.response.send_message(error or f"➕ Added **{player.current.title}** to **{name}**.", ephemeral=True)


@playlist_group.command(name="play", description="Add one of your saved playlists to the queue")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_play(interaction: discord.Interaction, name: str):
    saved = storage.get_playlist(interaction.user.id, name)
    if not saved:
        return await interaction.response.send_message(f"You don't have a playlist called **{name}**.", ephemeral=True)
    await interaction.response.defer()
    vc = await join_caller(interaction)
    if not vc:
        return
    player = get_player(interaction.guild.id)
    tracks = [Track(s.title, s.url, interaction.user.display_name, interaction.user.id, s.youtube_id, duration=s.duration)
              for s in saved]
    added = enqueue(player, tracks)
    await interaction.followup.send(f"📂 Added **{added}** songs from your playlist **{name}**.")
    if is_active(vc):
        await refresh_now_playing(interaction.guild)
    else:
        await play_next(interaction.guild)


@playlist_group.command(name="list", description="Show your saved playlists")
async def playlist_list(interaction: discord.Interaction):
    names = storage.list_playlists(interaction.user.id)
    if not names:
        return await interaction.response.send_message(
            "You have no saved playlists yet. Use `/playlist save` while music is playing.", ephemeral=True)
    lines = [f"• **{name}** · {count} songs" for name, count in names]
    await interaction.response.send_message("Your playlists:\n" + "\n".join(lines), ephemeral=True)


@playlist_group.command(name="show", description="Show the songs in one of your playlists")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_show(interaction: discord.Interaction, name: str):
    saved = storage.get_playlist(interaction.user.id, name)
    if saved is None:
        return await interaction.response.send_message(f"You don't have a playlist called **{name}**.", ephemeral=True)
    lines = [f"`{i}.` {s.title} · {format_duration(s.duration)}" for i, s in enumerate(saved[:20], start=1)]
    if len(saved) > 20:
        lines.append(f"...and {len(saved) - 20} more")
    await interaction.response.send_message(f"**{name}** ({len(saved)} songs)\n" + "\n".join(lines), ephemeral=True)


@playlist_group.command(name="delete", description="Delete one of your playlists")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_delete(interaction: discord.Interaction, name: str):
    deleted = storage.delete_playlist(interaction.user.id, name)
    await interaction.response.send_message(f"🗑️ Deleted **{name}**." if deleted
                                            else f"You don't have a playlist called **{name}**.", ephemeral=True)


tree.add_command(playlist_group)


# ---- commands: server settings ----

dj_group = app_commands.Group(name="dj", description="Choose who can control the music",
                              guild_only=True, default_permissions=discord.Permissions(manage_guild=True))


@dj_group.command(name="set", description="Only people with this role (and server managers) can control the music")
async def dj_set(interaction: discord.Interaction, role: discord.Role):
    player = get_player(interaction.guild.id)
    player.settings.dj_role_id = role.id
    storage.save_settings(player.settings)
    await interaction.response.send_message(
        f"🎧 DJ role set to {role.mention}. Others can still add songs and vote to skip.",
        allowed_mentions=discord.AllowedMentions.none())


@dj_group.command(name="clear", description="Let everyone control the music again")
async def dj_clear(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    player.settings.dj_role_id = None
    storage.save_settings(player.settings)
    await interaction.response.send_message("🎧 DJ role removed. Everyone can control the music.")


tree.add_command(dj_group)


@tree.command(name="24-7", description="Stay in the voice channel all the time, even when it's empty")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def stay_247(interaction: discord.Interaction, mode: Literal["on", "off"]):
    guild = interaction.guild
    player = get_player(guild.id)
    if mode == "off":
        set_247(player, False)
        await interaction.response.send_message("24/7 mode is off. I'll leave a minute after everyone else does.")
        return await refresh_now_playing(guild)

    vc = guild.voice_client
    voice = interaction.user.voice
    channel = vc.channel if vc else (voice.channel if voice else None)
    if not channel:
        return await interaction.response.send_message("Join a voice channel first, so I know where to stay.",
                                                       ephemeral=True)
    await interaction.response.defer()
    if not vc:
        try:
            await channel.connect(timeout=15, self_deaf=True)
        except Exception:
            log.exception("Could not join voice channel for 24/7")
            return await interaction.followup.send("I couldn't join that voice channel.")
    player.text_channel = interaction.channel
    set_247(player, True, channel, interaction.channel)
    await interaction.followup.send(f"♾️ 24/7 mode is on. I'll stay in {channel.mention}, and rejoin it after a restart.")
    await refresh_now_playing(guild)


def set_247(player: GuildPlayer, on: bool, channel=None, text_channel=None):
    s = player.settings
    s.stay_247 = on
    if on:
        s.voice_channel_id, s.text_channel_id = channel.id, text_channel.id
    storage.save_settings(s)


# ---- commands: profile and stats ----

def format_listen_time(seconds: float) -> str:
    """45m · 3h 20m · 4d 4h · 1w 1d"""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    weeks, days = divmod(days, 7)
    if weeks:
        return f"{weeks}w {days}d"
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _song_link(title: str, url: str | None) -> str:
    title = title.replace("[", "(").replace("]", ")")[:60]
    return f"[{title}]({url})" if url and url.startswith("http") else title


# Badges, earned automatically. For each kind only the highest tier reached is shown.
BADGE_TIERS = [
    (lambda p: p.total_seconds / 3600, [(1, "🎧 1h listener"), (10, "🎶 10h listener"), (100, "👑 100h listener")]),
    (lambda p: p.best_streak, [(7, "🔥 7-day streak"), (30, "☄️ 30-day streak"), (100, "🌟 100-day streak")]),
    (lambda p: p.songs_heard, [(100, "💯 100 songs"), (1000, "🏆 1,000 songs")]),
    (lambda p: p.songs_requested, [(50, "📀 50 requests"), (500, "💿 500 requests")]),
    (lambda p: len(p.listened_with), [(3, "👥 Social listener")]),
]


def earned_badges(p: storage.Profile) -> list[str]:
    badges = []
    for measure, tiers in BADGE_TIERS:
        value = measure(p)
        reached = [label for needed, label in tiers if value >= needed]
        if reached:
            badges.append(reached[-1])
    return badges


def profile_embed(member: discord.Member, guild: discord.Guild, p: storage.Profile, shared: bool,
                  style: storage.ProfileStyle | None = None) -> discord.Embed:
    """Text version of the profile, used if drawing the picture card fails."""
    style = style or storage.ProfileStyle()
    embed = discord.Embed(title=f"🎧 {member.display_name}'s music profile"[:256],
                          color=style.color if style.color is not None else 0x5865F2)
    embed.set_thumbnail(url=member.display_avatar.url)
    lines = [f"*{style.bio}*"] if style.bio else []
    if not p.total_seconds and not p.songs_requested:
        lines.append("No listening yet. Listen to music with Musica in a voice channel and your stats show up here.")
    embed.description = "\n\n".join(lines) or None
    if style.favorite:
        embed.add_field(name="⭐ Favorite song", value=style.favorite, inline=False)
    badges = earned_badges(p)
    if badges:
        embed.add_field(name="🏅 Badges", value=" · ".join(badges), inline=False)
    embed.add_field(name="Listening time", value=format_listen_time(p.total_seconds))
    embed.add_field(name="Songs", value=f"{p.songs_heard} heard · {p.songs_requested} requested")
    embed.add_field(name="Streak", value=f"🔥 {p.streak} day{'s' if p.streak != 1 else ''} · best {p.best_streak}")
    if p.top_songs:
        embed.add_field(name="Top songs", inline=False, value="\n".join(
            f"`{i}.` {_song_link(title, url)} · {plays}×" for i, (title, url, plays) in enumerate(p.top_songs, 1)))
    if p.top_artists:
        embed.add_field(name="Top artists", inline=False, value="\n".join(
            f"`{i}.` {artist} · {plays}×" for i, (artist, plays) in enumerate(p.top_artists, 1)))
    if p.listened_with:
        embed.add_field(name=f"Listened with most in {guild.name}"[:256], inline=False, value="\n".join(
            f"`{i}.` <@{uid}> · {format_listen_time(s)}" for i, (uid, s) in enumerate(p.listened_with, 1)))
    if p.top_servers:
        names = [f"{_server_name(gid)} · {format_listen_time(s)}" for gid, s in p.top_servers]
        embed.add_field(name="Top servers", inline=False,
                        value="\n".join(f"`{i}.` {n}" for i, n in enumerate(names, 1)))
    embed.set_footer(text="Type /profile or M.profile to show yours · /editprofile to decorate it")
    return embed


def _server_name(guild_id: int) -> str:
    guild = client.get_guild(guild_id)
    return guild.name if guild else "Unknown server"  # e.g. a server the bot was removed from


async def _display_name(guild: discord.Guild, user_id: int) -> str:
    user = guild.get_member(user_id) or client.get_user(user_id)
    if user is None:
        try:
            user = await client.fetch_user(user_id)
        except discord.HTTPException:
            return "Unknown user"
    return user.display_name


async def build_profile(member: discord.Member, guild: discord.Guild) -> tuple[discord.Embed | None, discord.File | None]:
    """The profile as a plain picture, like Jockie's: (None, file). If drawing fails: (text embed, None)."""
    p = storage.get_profile(member.id, guild.id)
    style = storage.get_style(member.id)
    servers = [(_server_name(gid), format_listen_time(seconds)) for gid, seconds in p.top_servers]
    accent = style.color if style.color is not None else 0x5865F2
    try:
        try:
            avatar = await member.display_avatar.with_size(256).with_static_format("png").read()
        except discord.HTTPException:
            avatar = None
        data = card.CardData(
            name=member.display_name, accent=accent, bio=style.bio, favorite=style.favorite, avatar=avatar,
            background=storage.load_background(member.id), listening=format_listen_time(p.total_seconds),
            songs_heard=p.songs_heard, songs_requested=p.songs_requested, streak=p.streak, best_streak=p.best_streak,
            badges=earned_badges(p), top_songs=[(title, plays) for title, _, plays in p.top_songs],
            top_artists=p.top_artists,
            listened_with=[(await _display_name(guild, uid), format_listen_time(s)) for uid, s in p.listened_with],
            top_servers=servers)
        jpeg = await asyncio.to_thread(card.render_profile, data)
    except Exception:
        log.exception("Could not draw the profile card")
        return profile_embed(member, guild, p, True, style), None
    return None, discord.File(io.BytesIO(jpeg), filename="profile.jpg")


NOSY_REPLIES = [
    "🕵️ Nice try, detective! **{name}**'s profile is top secret. Only they can show it, by typing `M.profile` themselves.",
    "🔒 Whoa there! That's **{name}**'s private info. Ask them nicely and maybe they'll show you 👀",
    "🙈 Peeking at other people's profiles? Not on my watch. **{name}** can show theirs with `M.profile`.",
    "🚫 Access denied! **{name}**'s music taste is classified. 🎧",
    "😏 Curious about **{name}**'s secret playlist? Me too, but rules are rules. They have to show it themselves.",
]
BOT_PROFILE_REPLY = "🤖 Me? My only stat is *infinite music*. 🎶 Try `M.profile` on yourself!"


@tree.command(description="Show your music profile card: listening time, top songs, badges, who you listen with")
@app_commands.describe(user="Only you can show your own profile 😉")
async def profile(interaction: discord.Interaction, user: discord.User | None = None):
    member = interaction.user
    if user is not None and user.id != member.id:
        text = BOT_PROFILE_REPLY if getattr(user, "bot", False) else \
            random.choice(NOSY_REPLIES).format(name=getattr(user, "display_name", None) or "that person")
        return await interaction.response.send_message(text, ephemeral=True)
    if not storage.is_tracking(member.id):
        return await interaction.response.send_message(
            "Tracking is off for you, so there's no profile. Turn it on with `/tracking on`.", ephemeral=True)
    await interaction.response.defer(thinking=True)
    embed, file = await build_profile(member, interaction.guild)
    content = {"file": file} if file else {"embed": embed}
    await interaction.followup.send(allowed_mentions=discord.AllowedMentions.none(), **content)


def _no_links(text: str) -> bool:
    return not re.search(r"https?://|discord\.gg/|www\.", text, re.I)


def parse_color(text: str) -> int | None:
    """'#ff66aa', 'ff66aa' or the short '#f6a' -> 0xff66aa"""
    match = re.fullmatch(r"#?([0-9a-f]{3}|[0-9a-f]{6})", text.strip().lower())
    if not match:
        return None
    code = match.group(1)
    if len(code) == 3:
        code = "".join(c * 2 for c in code)
    return int(code, 16)


async def save_background_upload(user_id: int, image: discord.Attachment) -> str | None:
    """Store an uploaded background. Returns an error message, or None when it worked."""
    if image.size > card.MAX_BACKGROUND_BYTES or not (image.content_type or "").startswith("image/"):
        return "Use a PNG, JPG, WebP or GIF picture up to 10 MB."
    try:
        jpeg = await asyncio.to_thread(card.prepare_background, await image.read())
    except discord.HTTPException:
        jpeg = None
    if not jpeg:
        return "I couldn't read that picture. Try a different PNG or JPG."
    storage.save_background(user_id, jpeg)
    return None


class _TextModal(discord.ui.Modal):
    """A one-box form for the bio, favorite song or a custom color."""

    def __init__(self, editor: "ProfileEditor", title: str, label: str, current: str | None, max_length: int,
                 placeholder: str, save):
        super().__init__(title=title, timeout=300)
        self.editor, self.save = editor, save
        self.box = discord.ui.TextInput(
            default=current, required=False, max_length=max_length, placeholder=placeholder,
            style=discord.TextStyle.paragraph if max_length > 80 else discord.TextStyle.short)
        self.add_item(discord.ui.Label(text=label, component=self.box))

    async def on_submit(self, interaction: discord.Interaction):
        error = self.save(self.box.value.strip())
        await self.editor.refresh(interaction, error)


class _BackgroundModal(discord.ui.Modal, title="Profile background"):
    def __init__(self, editor: "ProfileEditor"):
        super().__init__(timeout=300)
        self.editor = editor
        self.upload = discord.ui.FileUpload(required=True, max_values=1)
        self.add_item(discord.ui.Label(text="Picture", description="PNG, JPG, WebP or GIF, up to 10 MB",
                                       component=self.upload))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        error = await save_background_upload(self.editor.owner_id, self.upload.values[0])
        await self.editor.refresh(interaction, error)


class ProfileEditor(discord.ui.View):
    """The /editprofile panel: a live preview of the card, with buttons to change it."""

    def __init__(self, owner: discord.Member, guild: discord.Guild):
        super().__init__(timeout=600)
        self.owner, self.owner_id, self.guild = owner, owner.id, guild
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"This editor belongs to **{self.owner.display_name}**. Open your own with `M.editprofile` 🙂",
                ephemeral=True)
            return False
        return True

    async def render(self) -> dict:
        """The panel's message: a line of text, the preview picture, and these controls."""
        embed, file = await build_profile(self.owner, self.guild)
        return {"content": "✏️ **Edit your profile.** Pick what to change below. Changes are saved right away.",
                "attachments": [file] if file else [], "embed": embed, "view": self}

    async def refresh(self, interaction: discord.Interaction, error: str | None = None):
        """Redraw the preview after a change, or say what went wrong."""
        if not interaction.response.is_done():
            await interaction.response.defer()
        if error:
            return await interaction.followup.send(error, ephemeral=True)
        await interaction.edit_original_response(**await self.render())

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="✅ Profile editor closed. Type `M.editprofile` to open it again.",
                                        view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Color", emoji="🎨", style=discord.ButtonStyle.primary, row=0)
    async def color(self, interaction: discord.Interaction, button: discord.ui.Button):
        def save(text: str):
            if not text:
                storage.update_style(self.owner_id, color=None)
                return None
            color = parse_color(text)
            if color is None:
                return "That's not a color code. Use one like `#ff66aa` (or the short `#f6a`)."
            storage.update_style(self.owner_id, color=color)
            return None

        current = storage.get_style(self.owner_id).color
        await interaction.response.send_modal(_TextModal(
            self, "Card color", "Color code (leave empty for the default)",
            f"#{current:06x}" if current is not None else None, 7, "#ff66aa", save))

    @discord.ui.button(label="Bio", emoji="💬", style=discord.ButtonStyle.primary, row=0)
    async def bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        def save(text: str):
            if text and not _no_links(text):
                return "Links aren't allowed in bios."
            storage.update_style(self.owner_id, bio=discord.utils.escape_mentions(text) or None)
            return None

        await interaction.response.send_modal(_TextModal(
            self, "Your bio", "Bio (leave empty to remove it)", storage.get_style(self.owner_id).bio, 100,
            "lofi all day 🌙", save))

    @discord.ui.button(label="Favorite song", emoji="⭐", style=discord.ButtonStyle.primary, row=0)
    async def favorite(self, interaction: discord.Interaction, button: discord.ui.Button):
        def save(text: str):
            if text and not _no_links(text):
                return "Just type the song's name, without a link."
            storage.update_style(self.owner_id, favorite=discord.utils.escape_mentions(text) or None)
            return None

        await interaction.response.send_modal(_TextModal(
            self, "Favorite song", "Favorite song (leave empty to remove it)",
            storage.get_style(self.owner_id).favorite, 80, "Get Lucky - Daft Punk", save))

    @discord.ui.button(label="Background", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def background(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_BackgroundModal(self))

    @discord.ui.button(label="Remove background", emoji="🗑️", style=discord.ButtonStyle.secondary, row=1)
    async def remove_background(self, interaction: discord.Interaction, button: discord.ui.Button):
        storage.delete_background(self.owner_id)
        await self.refresh(interaction)

    @discord.ui.button(label="Reset everything", emoji="🧽", style=discord.ButtonStyle.danger, row=1)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        storage.reset_style(self.owner_id)
        await self.refresh(interaction)

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="✅ Profile saved! Show it off with `M.profile`.", view=None)


@tree.command(name="editprofile", description="Open the profile editor: color, bio, favorite song, background")
async def edit_profile(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    editor = ProfileEditor(interaction.user, interaction.guild)
    panel = await editor.render()
    files = panel.pop("attachments")
    if panel["embed"] is None:
        del panel["embed"]
    if files:
        panel["file"] = files[0]
    editor.message = await interaction.followup.send(ephemeral=True, wait=True, **panel)


@tree.command(description="Turn listening stats on or off for you (off also deletes your stats)")
async def tracking(interaction: discord.Interaction, mode: Literal["on", "off"]):
    storage.set_tracking(interaction.user.id, mode == "on")
    await interaction.response.send_message(
        "📊 Tracking is on. Your listening stats will show up in `/profile`." if mode == "on" else
        "📊 Tracking is off, and your listening stats were deleted. Your playlists are kept.", ephemeral=True)


@tree.command(description="This server's most played songs and biggest listeners in the last 7 days")
async def top(interaction: discord.Interaction):
    songs, people = storage.server_top(interaction.guild.id)
    if not songs and not people:
        return await interaction.response.send_message("No music was played here in the last 7 days.", ephemeral=True)
    embed = discord.Embed(title=f"🏆 Top in {interaction.guild.name} · last 7 days"[:256], color=0xFAA61A)
    if songs:
        embed.add_field(name="Most played", inline=False, value="\n".join(
            f"`{i}.` {_song_link(title, url)} · {n} play{'s' if n != 1 else ''}"
            for i, (title, url, n) in enumerate(songs, 1)))
    if people:
        embed.add_field(name="Biggest listeners", inline=False, value="\n".join(
            f"`{i}.` <@{uid}> · {format_listen_time(s)}" for i, (uid, s) in enumerate(people, 1)))
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@tree.command(description="The last 20 songs played in this server")
async def history(interaction: discord.Interaction):
    rows = storage.recent_plays(interaction.guild.id)
    if not rows:
        return await interaction.response.send_message("Nothing has been played here yet.", ephemeral=True)
    lines = [f"<t:{played_at}:R> {_song_link(title, url)}" + (f" · <@{req}>" if req else "")
             for title, url, req, played_at in rows]
    embed = discord.Embed(title="🕘 Recently played", description="\n".join(lines)[:4096], color=0x5865F2)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@tree.command(description="Send the current song to your DMs so you can find it later")
async def grab(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if not player or not player.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    track = player.current
    embed = discord.Embed(title=track.title[:256], url=track.url if str(track.url).startswith("http") else None,
                          color=0x5865F2)
    embed.set_author(name="🎵 Saved from Musica")
    embed.add_field(name="Length", value=format_duration(track.duration))
    embed.add_field(name="Heard in", value=interaction.guild.name)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    try:
        await interaction.user.send(embed=embed)
    except discord.HTTPException:
        return await interaction.response.send_message(f"I can't DM you, so here it is: {track.url}", ephemeral=True)
    await interaction.response.send_message(f"📬 Sent **{track.title}** to your DMs.", ephemeral=True)


# ---- commands: help and privacy ----

HELP_SECTIONS = {
    "🎵 Playing": [
        ("/play", "Play a song or playlist. Suggestions appear while you type"),
        ("/skip", "Skip (others vote unless you're a DJ or added the song)"),
        ("/previous", "Go back to the previous song"),
        ("/pause · /resume", "Pause or resume"),
        ("/seek", "Jump to a time, like `1:30`"),
        ("/filter", "Audio effects: bass boost, nightcore, slowed + reverb, 8D"),
        ("/volume", "Set the volume (remembered for this server)"),
        ("/lyrics", "Lyrics of the current song, or any song"),
        ("/stop", "Stop and leave"),
    ],
    "📜 Queue": [
        ("/queue · /nowplaying", "See the queue, or bring the player card back"),
        ("/remove · /move · /skipto", "Edit the queue"),
        ("/shuffle · /clear", "Shuffle or empty the queue"),
        ("/loop", "Repeat the song or the whole queue"),
        ("/autoplay", "Keep playing similar songs when the queue ends"),
    ],
    "👤 You": [
        ("/profile", "Show your profile card: listening time, top songs and artists, streak, badges"),
        ("/editprofile", "Open the editor: color, bio, favorite song, background picture"),
        ("/grab", "Send the current song to your DMs"),
        ("/tracking", "Turn your listening stats on or off"),
    ],
    "🏆 This server": [
        ("/top", "Most played songs and biggest listeners this week"),
        ("/history", "The last 20 songs played here"),
    ],
    "💾 Your playlists": [
        ("/playlist save · add", "Save the queue, or add the current song"),
        ("/playlist play · show · list · delete", "Use and manage your playlists"),
    ],
    "⚙️ Server (managers)": [
        ("/dj set · clear", "Only this role can control the music"),
        ("/24-7", "Stay in the voice channel all the time"),
    ],
    "🔒 Privacy": [
        ("/deletemydata", "Delete everything the bot saved about you"),
    ],
}


@tree.command(name="help", description="Show all commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="🎵 Musica commands", color=0x5865F2,
                          description="Join a voice channel and use `/play`. The player card has buttons for "
                                      "back, pause, skip, stop, loop, shuffle, autoplay, 24/7, queue and lyrics.\n\n"
                                      f"Every command also works with the **`{PREFIX}`** prefix, like `{PREFIX}play`. "
                                      f"Short versions: `{PREFIX}p` play · `{PREFIX}s` skip · `{PREFIX}q` queue · "
                                      f"`{PREFIX}np` now playing · `{PREFIX}back` previous · `{PREFIX}vol` volume · "
                                      f"`{PREFIX}l` loop · `{PREFIX}ap` autoplay · `{PREFIX}ly` lyrics · `{PREFIX}f` filter · "
                                      f"`{PREFIX}sh` shuffle · `{PREFIX}rm` remove · `{PREFIX}pl` playlist · "
                                      f"`{PREFIX}dc` stop · `{PREFIX}me` profile · `{PREFIX}g` grab · `{PREFIX}h` help")
    for section, commands in HELP_SECTIONS.items():
        embed.add_field(name=section, value="\n".join(f"`{c}` {d}" for c, d in commands), inline=False)
    embed.add_field(name="Links", value=f"[Terms of Service]({TERMS_URL}) · [Privacy Policy]({PRIVACY_URL}) · "
                                        f"[Source code]({REPO_URL})", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


class ConfirmDelete(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:  # with M.deletemydata the buttons are visible to everyone
            await interaction.response.send_message("These buttons are for someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete my data", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        count = storage.delete_user_data(self.user_id)
        await interaction.response.edit_message(
            content=f"🗑️ Done. Deleted {count} saved playlist(s), your listening stats, profile decorations and background. "
                    "Nothing else about you is stored.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. Nothing was deleted.", view=None)


@tree.command(name="deletemydata", description="Delete everything the bot has saved about you")
async def delete_my_data(interaction: discord.Interaction):
    names = storage.list_playlists(interaction.user.id)
    await interaction.response.send_message(
        f"This deletes your {len(names)} saved playlist(s), your listening stats, profile decorations and background. That's all the data the bot "
        f"keeps about you ([Privacy Policy]({PRIVACY_URL})). This can't be undone.",
        view=ConfirmDelete(interaction.user.id), ephemeral=True)


# ---- prefix commands: M.play, M.p, M.skip, ... ----

PREFIX = "M."
PREFIX_ALIASES = {
    "p": "play", "s": "skip", "next": "skip", "back": "previous", "prev": "previous", "np": "nowplaying",
    "q": "queue", "vol": "volume", "v": "volume", "l": "loop", "ap": "autoplay", "ly": "lyrics", "f": "filter",
    "leave": "stop", "dc": "stop", "rm": "remove", "sh": "shuffle", "pl": "playlist", "h": "help", "247": "24-7",
    "pf": "profile", "me": "profile", "g": "grab", "hist": "history",
}


class PrefixInteraction:
    """Lets a prefix command run the slash command code: replies go to the channel instead of the interaction."""

    def __init__(self, message: discord.Message, command: app_commands.Command):
        self.message = message
        self.user = message.author
        self.guild = message.guild
        self.channel = message.channel
        self.command = command
        self.response = _PrefixResponse(self)
        self.followup = _PrefixFollowup(self)
        self._last: discord.Message | None = None

    async def _send(self, content=None, *, embed=None, view=None, file=None, ephemeral=False, allowed_mentions=None,
                    **_):
        kwargs = {"content": content, "embed": embed,
                  "allowed_mentions": allowed_mentions or discord.AllowedMentions.none()}
        if file is not None:
            kwargs["file"] = file
        if view is not None:
            kwargs["view"] = view
        elif ephemeral and embed is None:
            kwargs["delete_after"] = 30  # stand-in for "only you can see this": tidy it up later
        self._last = await self.channel.send(**kwargs)  # a normal message, not a reply
        return self._last

    async def original_response(self) -> discord.Message | None:
        return self._last


class _PrefixResponse:
    def __init__(self, interaction: PrefixInteraction):
        self._interaction = interaction
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True
        return await self._interaction._send(content, **kwargs)

    async def defer(self, **_):
        self._done = True
        try:
            await self._interaction.channel.typing()
        except discord.HTTPException:
            pass


class _PrefixFollowup:
    def __init__(self, interaction: PrefixInteraction):
        self._interaction = interaction

    async def send(self, content=None, **kwargs):
        return await self._interaction._send(content, **kwargs)


def prefix_usage(cmd: app_commands.Command) -> str:
    args = " ".join(f"<{p.display_name}>" if p.required else f"[{p.display_name}]" for p in cmd.parameters)
    return f"{PREFIX}{cmd.qualified_name} {args}".strip()


def _convert_prefix_arg(guild: discord.Guild, cmd: app_commands.Command, p: app_commands.Parameter, token: str,
                        mentions=()):
    if p.choices:
        for choice in p.choices:
            if token.lower() in (choice.name.lower(), str(choice.value).lower()):
                wants_choice = getattr(cmd.callback.__annotations__.get(p.name), "__origin__", None) is app_commands.Choice
                return choice if wants_choice else choice.value
        raise ValueError(f"`{p.display_name}` must be one of: {', '.join(str(c.value) for c in p.choices)}.")
    if p.type is discord.AppCommandOptionType.boolean:
        if token.lower() in ("true", "yes", "on", "1", p.name.lower()):  # typing the option's name means yes
            return True
        if token.lower() in ("false", "no", "off", "0"):
            return False
        raise ValueError(f"`{p.display_name}` must be yes or no.")
    if p.type is discord.AppCommandOptionType.integer:
        try:
            value = int(token)
        except ValueError:
            raise ValueError(f"`{p.display_name}` must be a number.") from None
        if (p.min_value is not None and value < p.min_value) or (p.max_value is not None and value > p.max_value):
            raise ValueError(f"`{p.display_name}` must be between {p.min_value} and {p.max_value if p.max_value is not None else '∞'}.")
        return value
    if p.type is discord.AppCommandOptionType.user:
        user_id = token.strip("<@!>")
        found = next((m for m in mentions if str(m.id) == user_id), None) if user_id.isdigit() else None
        found = found or (guild.get_member(int(user_id)) if user_id.isdigit() else None)
        if not found:
            raise ValueError("Mention someone, like @name.")
        return found
    if p.type is discord.AppCommandOptionType.role:
        role_id = token.strip("<@&>")
        role = guild.get_role(int(role_id)) if role_id.isdigit() else \
            discord.utils.find(lambda r: r.name.lower() == token.lstrip("@").lower(), guild.roles)
        if not role:
            raise ValueError(f"I couldn't find the role `{token}`.")
        return role
    if p.max_value is not None and len(token) > p.max_value:
        raise ValueError(f"`{p.display_name}` can be at most {p.max_value} characters.")
    return token


def parse_prefix_args(guild: discord.Guild, cmd: app_commands.Command, text: str,
                      attachments: list[discord.Attachment] = (), mentions: list = ()) -> dict:
    """Turn 'M.move 3 1' into {'from_position': 3, 'to_position': 1}. The last text option takes the rest,
    and file options take the files attached to the message."""
    kwargs, rest = {}, text.strip()
    files = list(attachments)
    text_params = [i for i, p in enumerate(cmd.parameters) if p.type is discord.AppCommandOptionType.string]
    last_text = text_params[-1] if text_params else -1
    for i, p in enumerate(cmd.parameters):
        if p.type is discord.AppCommandOptionType.attachment:
            if files:
                kwargs[p.name] = files.pop(0)
            elif p.required:
                raise ValueError(f"Attach a file for `{p.display_name}`.")
            continue
        if not rest:
            if p.required:
                raise ValueError(f"Missing `{p.display_name}`.")
            continue
        if i == last_text:
            token, rest = rest, ""
        else:
            token, _, rest = rest.partition(" ")
            rest = rest.strip()
        kwargs[p.name] = _convert_prefix_arg(guild, cmd, p, token, mentions)
    return kwargs


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild or not message.content[:len(PREFIX)].lower() == PREFIX.lower():
        return
    words = message.content[len(PREFIX):].split(maxsplit=1)
    if not words:
        return
    cmd = tree.get_command(PREFIX_ALIASES.get(words[0].lower(), words[0].lower()))
    rest = words[1] if len(words) > 1 else ""
    if isinstance(cmd, app_commands.Group):
        sub_words = rest.split(maxsplit=1)
        sub = cmd.get_command(sub_words[0].lower()) if sub_words else None
        if not sub:
            return await message.channel.send(f"Use `{PREFIX}{cmd.name} {'|'.join(c.name for c in cmd.commands)}`.",
                                              delete_after=30)
        cmd, rest = sub, sub_words[1] if len(sub_words) > 1 else ""
    if not isinstance(cmd, app_commands.Command):
        return  # not one of our commands (e.g. someone typing "M.Sc"): stay quiet

    required = cmd.default_permissions or (cmd.parent.default_permissions if cmd.parent else None)
    if required and not message.author.guild_permissions >= required:
        return await message.channel.send("Only server managers can use that.", delete_after=30)
    try:
        kwargs = parse_prefix_args(message.guild, cmd, rest, message.attachments, message.mentions)
    except ValueError as e:
        return await message.channel.send(f"{e}\nUsage: `{prefix_usage(cmd)}`", delete_after=30)

    try:
        await cmd.callback(PrefixInteraction(message, cmd), **kwargs)
    except Exception:
        log.exception("Prefix command %s failed", cmd.qualified_name)
        try:
            await message.channel.send("Something went wrong. Please try again.", delete_after=30)
        except discord.HTTPException:
            pass


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error("Command /%s failed", interaction.command.name if interaction.command else "?", exc_info=error)
    try:
        await reply(interaction, "Something went wrong. Please try again.")
    except discord.HTTPException:
        pass


# ---- events and background tasks ----

@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    if member.id == client.user.id:
        if before.channel and after.channel is None:  # the bot was disconnected (kicked or /stop)
            player = players.pop(guild.id, None)
            if player:
                await retire_now_playing(player)
        return
    # Leave when everyone else has left the voice channel for a minute (unless 24/7 mode is on).
    vc = guild.voice_client
    if not vc or before.channel != vc.channel or listeners(vc) > 0:
        return
    await asyncio.sleep(60)
    vc = guild.voice_client
    settings = players[guild.id].settings if guild.id in players else storage.load_settings(guild.id)
    if vc and vc.is_connected() and listeners(vc) == 0 and not settings.stay_247:
        await stop_playback(guild)


@client.event
async def on_guild_remove(guild: discord.Guild):
    """Forget a server's settings when the bot is removed from it."""
    players.pop(guild.id, None)
    storage.delete_settings(guild.id)


@tasks.loop(seconds=15)
async def update_progress():
    """Move the progress bar on each server's player card."""
    for guild_id, player in list(players.items()):
        guild = client.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if player.np_message and player.current and vc and vc.is_playing():
            try:
                await player.np_message.edit(embed=now_playing_embed(player))
            except discord.NotFound:
                player.np_message = None
            except discord.HTTPException:
                pass


def active_listeners(vc: discord.VoiceClient) -> list[int]:
    """People in the channel who can hear the music: not bots, not deafened."""
    return [m.id for m in vc.channel.members
            if not m.bot and not (m.voice and (m.voice.self_deaf or m.voice.deaf))]


@tasks.loop(seconds=15)
async def track_listening():
    """Add listening time for everyone hearing the music (skipped for people who turned tracking off)."""
    now = time.monotonic()
    for guild_id, player in list(players.items()):
        guild = client.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not (vc and vc.is_playing() and player.current):
            player.last_stats_tick = None
            continue
        last, player.last_stats_tick = player.last_stats_tick, now
        if last is None:
            continue
        seconds = min(now - last, 30.0)
        users = active_listeners(vc)
        newly_heard = set()
        for user_id in users:
            before = player.listened.get(user_id, 0.0)
            player.listened[user_id] = before + seconds
            if before < storage.HEARD_AFTER_SECONDS <= before + seconds:
                newly_heard.add(user_id)
        try:
            storage.record_listening(guild_id, player.current.song_info(), users, seconds, newly_heard)
        except Exception:
            log.exception("Could not record listening stats")


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


@tasks.loop(seconds=30)
async def keep_awake_while_playing():
    """On Windows, stop the PC from going to sleep while music is playing (like a video player does)."""
    playing = any(vc.is_playing() for vc in client.voice_clients)
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if playing else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


COMMANDS_HASH_FILE = BASE_DIR / ".commands_hash"


async def sync_commands_if_changed():
    """Only send the slash command list to Discord when it changed, since Discord rate-limits syncing."""
    payload = sorted((c.to_dict(tree) for c in tree.get_commands()), key=lambda c: c["name"])
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    try:
        if COMMANDS_HASH_FILE.read_text().strip() == digest:
            log.info("Slash commands unchanged, skipping sync.")
            return
    except OSError:
        pass
    await tree.sync()
    COMMANDS_HASH_FILE.write_text(digest)
    log.info("Slash commands synced with Discord.")


async def rejoin_247_channels():
    """After a restart, go back to the voice channels of servers that have 24/7 mode on."""
    for s in storage.servers_with_247():
        guild = client.get_guild(s.guild_id)
        if not guild or guild.voice_client:
            continue
        channel = guild.get_channel(s.voice_channel_id) if s.voice_channel_id else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            continue
        try:
            await channel.connect(timeout=15, self_deaf=True)
        except Exception:
            log.warning("Could not rejoin 24/7 channel in %s", guild.name)
            continue
        text = guild.get_channel(s.text_channel_id) if s.text_channel_id else None
        if text:
            get_player(guild.id).text_channel = text
        log.info("Rejoined 24/7 channel in %s", guild.name)


@client.event
async def on_ready():
    if sys.platform == "win32" and not keep_awake_while_playing.is_running():
        keep_awake_while_playing.start()
    if not update_progress.is_running():
        update_progress.start()
    if not track_listening.is_running():
        track_listening.start()
    storage.prune_old_plays()
    client.add_view(PlayerControls())  # makes buttons on existing cards work again after a restart
    log.info("Logged in as %s.", client.user)
    await sync_commands_if_changed()
    await rejoin_247_channels()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in a .env file next to bot.py")
    storage.init()
    try:
        client.run(TOKEN, log_handler=log_handler, root_logger=True)
    except discord.PrivilegedIntentsRequired:
        log.error("Discord refused to connect: turn on MESSAGE CONTENT INTENT in the Developer Portal "
                  "(Applications > Musica > Bot > Privileged Gateway Intents), then restart the bot.")
        raise
