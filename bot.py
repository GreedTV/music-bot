"""Discord music bot: plays YouTube (and other sites) by link or search, with a queue and autoplay."""
import asyncio
import logging
import logging.handlers
import os
import random
import time
from collections import deque
from pathlib import Path
from typing import Literal

import discord
import imageio_ffmpeg
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")

# Log to bot.log (rotated at 1 MB) so the bot can run in the background with no window.
log_handler = logging.handlers.RotatingFileHandler(
    BASE_DIR / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
log = logging.getLogger("musicbot")

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
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

STREAM_MAX_AGE = 60 * 60  # YouTube stream URLs expire after a few hours; re-resolve older ones
MAX_QUEUE = 500


class Track:
    def __init__(self, title: str, url: str, requester: str, youtube_id: str | None = None,
                 stream_url: str | None = None):
        self.title = title
        self.url = url  # page URL, used to (re)resolve the audio stream
        self.requester = requester
        self.youtube_id = youtube_id
        self.stream_url = stream_url
        self.resolved_at = time.time() if stream_url else 0.0


class GuildPlayer:
    """Per-server queue and playback state."""

    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop: Literal["off", "song", "queue"] = "off"
        self.autoplay = True
        self.volume = 0.5
        self.history: deque[str] = deque(maxlen=100)  # recently played YouTube ids, so autoplay avoids repeats
        self.text_channel: discord.abc.Messageable | None = None
        self.skipping = False
        self.lock = asyncio.Lock()


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
players: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    return players.setdefault(guild_id, GuildPlayer())


def _youtube_id(info: dict) -> str | None:
    key = (info.get("extractor_key") or info.get("ie_key") or "").lower()
    return info.get("id") if key.startswith("youtube") else None


def _entry_to_track(entry: dict, requester: str) -> Track:
    url = entry.get("webpage_url") or entry.get("url")
    yt_id = _youtube_id(entry)
    if yt_id and not str(url).startswith("http"):
        url = f"https://www.youtube.com/watch?v={yt_id}"
    return Track(entry.get("title") or url, url, requester, yt_id)


async def fetch_tracks(query: str, requester: str) -> tuple[list[Track], str | None]:
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
            return [_entry_to_track(entries[0], requester)], None
        return [_entry_to_track(e, requester) for e in entries], info.get("title") or "playlist"
    track = Track(info["title"], info.get("webpage_url", query), requester, _youtube_id(info), info.get("url"))
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
    return track.stream_url


async def find_related(seed: Track, player: GuildPlayer) -> Track | None:
    """Pick a song from YouTube's Mix for the seed track (similar songs and similar artists)."""
    seed_id = seed.youtube_id
    if not seed_id:  # e.g. a SoundCloud track: find the same song on YouTube first
        found, _ = await fetch_tracks(f"ytsearch1:{seed.title}", "Autoplay")
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
    return _entry_to_track(random.choice(fresh[:5]), "Autoplay")


def listeners(vc: discord.VoiceClient) -> int:
    return sum(1 for m in vc.channel.members if not m.bot)


async def announce(player: GuildPlayer, text: str):
    if player.text_channel:
        try:
            await player.text_channel.send(text)
        except discord.HTTPException:
            log.warning("Could not send message to text channel")


async def play_next(guild: discord.Guild):
    """Start the next track: loop / queue / autoplay, in that order."""
    player = get_player(guild.id)
    async with player.lock:
        vc = guild.voice_client
        if not vc or not vc.is_connected() or vc.is_playing() or vc.is_paused():
            return

        finished, player.current = player.current, None
        if finished and not player.skipping:
            if player.loop == "song":
                player.queue.appendleft(finished)
            elif player.loop == "queue":
                player.queue.append(finished)
        elif finished and player.skipping and player.loop == "queue":
            player.queue.append(finished)
        player.skipping = False

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
                await announce(player, f"🔁 Autoplay: **{track.title}**")
            else:
                return

            try:
                stream = await resolve_stream(track)
            except Exception as e:
                log.warning("Could not play %s: %s", track.url, e)
                await announce(player, f"⚠️ Couldn't play **{track.title}**, skipping.")
                finished = track
                continue
            break
        else:
            await announce(player, "⚠️ Several songs in a row failed to load. Stopping for now.")
            return

        player.current = track
        if track.youtube_id:
            player.history.append(track.youtube_id)
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(stream, executable=FFMPEG_EXE, **FFMPEG_OPTS), volume=player.volume
        )

        def after(err):
            if err:
                log.error("Playback error: %s", err)
            asyncio.run_coroutine_threadsafe(play_next(guild), client.loop)

        vc.play(source, after=after)
        if track.requester != "Autoplay":
            await announce(player, f"▶️ Now playing **{track.title}** (requested by {track.requester})")


def is_active(vc: discord.VoiceClient | None) -> bool:
    return bool(vc and (vc.is_playing() or vc.is_paused()))


@tree.command(description="Play a song or playlist from a link (YouTube, SoundCloud, ...) or search terms")
@app_commands.describe(query="Link or search terms")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)

    await interaction.response.defer()
    player = get_player(interaction.guild.id)
    player.text_channel = interaction.channel
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    try:
        tracks, playlist = await fetch_tracks(query, interaction.user.display_name)
    except Exception as e:
        log.warning("Search failed for %r: %s", query, e)
        return await interaction.followup.send("Couldn't find anything for that.")

    room = MAX_QUEUE - len(player.queue)
    tracks = tracks[:room]
    if not tracks:
        return await interaction.followup.send(f"The queue is full ({MAX_QUEUE} songs).")
    player.queue.extend(tracks)

    if playlist:
        msg = f"Added **{len(tracks)}** songs from **{playlist}** to the queue."
    elif is_active(vc):
        msg = f"Queued **{tracks[0].title}** (position {len(player.queue)})"
    else:
        msg = f"Loading **{tracks[0].title}**..."
    await interaction.followup.send(msg)
    if not is_active(vc):
        await play_next(interaction.guild)


@tree.command(description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not is_active(vc):
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    get_player(interaction.guild.id).skipping = True
    vc.stop()  # triggers the after-callback, which plays the next track
    await interaction.response.send_message("⏭️ Skipped.")


@tree.command(description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message("⏸️ Paused.")
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message("▶️ Resumed.")
    await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@tree.command(description="Show the queue")
@app_commands.describe(page="Page number")
async def queue(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
    player = get_player(interaction.guild.id)
    status = f"Loop: **{player.loop}** · Autoplay: **{'on' if player.autoplay else 'off'}**"
    if not player.current and not player.queue:
        return await interaction.response.send_message(f"The queue is empty.\n{status}")
    per_page = 10
    pages = max(1, -(-len(player.queue) // per_page))
    page = min(page, pages)
    lines = []
    if player.current:
        lines.append(f"**Now playing:** {player.current.title} (requested by {player.current.requester})")
    start = (page - 1) * per_page
    for i, t in enumerate(list(player.queue)[start:start + per_page], start=start + 1):
        lines.append(f"`{i}.` {t.title} (requested by {t.requester})")
    if player.queue:
        lines.append(f"Page {page}/{pages} · {len(player.queue)} songs queued")
    lines.append(status)
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="nowplaying", description="Show the current song")
async def now_playing(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if not player.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    t = player.current
    await interaction.response.send_message(f"▶️ **{t.title}** (requested by {t.requester})\n{t.url}")


@tree.command(description="Remove a song from the queue")
@app_commands.describe(position="Position in /queue")
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    player = get_player(interaction.guild.id)
    if position > len(player.queue):
        return await interaction.response.send_message("There's no song at that position.", ephemeral=True)
    track = player.queue[position - 1]
    del player.queue[position - 1]
    await interaction.response.send_message(f"Removed **{track.title}**.")


@tree.command(description="Move a song to a different position in the queue")
async def move(interaction: discord.Interaction, from_position: app_commands.Range[int, 1],
               to_position: app_commands.Range[int, 1]):
    player = get_player(interaction.guild.id)
    if from_position > len(player.queue):
        return await interaction.response.send_message("There's no song at that position.", ephemeral=True)
    track = player.queue[from_position - 1]
    del player.queue[from_position - 1]
    player.queue.insert(min(to_position, len(player.queue) + 1) - 1, track)
    await interaction.response.send_message(f"Moved **{track.title}** to position {min(to_position, len(player.queue))}.")


@tree.command(description="Skip straight to a song in the queue")
@app_commands.describe(position="Position in /queue")
async def skipto(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    player = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    if position > len(player.queue) or not vc:
        return await interaction.response.send_message("There's no song at that position.", ephemeral=True)
    for _ in range(position - 1):
        dropped = player.queue.popleft()
        if player.loop == "queue":
            player.queue.append(dropped)
    await interaction.response.send_message(f"⏭️ Skipping to **{player.queue[0].title}**.")
    player.skipping = True
    if is_active(vc):
        vc.stop()
    else:
        await play_next(interaction.guild)


@tree.command(description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if len(player.queue) < 2:
        return await interaction.response.send_message("Not enough songs to shuffle.", ephemeral=True)
    items = list(player.queue)
    random.shuffle(items)
    player.queue = deque(items)
    await interaction.response.send_message("🔀 Shuffled the queue.")


@tree.command(description="Clear upcoming songs (keeps the current one playing)")
async def clear(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    player.queue.clear()
    await interaction.response.send_message("🧹 Cleared the queue.")


@tree.command(description="Repeat the current song, the whole queue, or nothing")
async def loop(interaction: discord.Interaction, mode: Literal["off", "song", "queue"]):
    get_player(interaction.guild.id).loop = mode
    await interaction.response.send_message(f"🔁 Loop set to **{mode}**.")


@tree.command(description="When the queue ends, keep playing similar songs")
async def autoplay(interaction: discord.Interaction, mode: Literal["on", "off"]):
    get_player(interaction.guild.id).autoplay = mode == "on"
    await interaction.response.send_message(f"Autoplay is now **{mode}**.")


@tree.command(description="Set volume (0-100)")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
    player = get_player(interaction.guild.id)
    player.volume = percent / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to {percent}%.")


@tree.command(description="Stop, clear the queue, and leave the voice channel")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    players.pop(interaction.guild.id, None)
    if vc:
        await vc.disconnect()
        return await interaction.response.send_message("⏹️ Stopped and left the channel.")
    await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Leave when everyone else has left the voice channel for a minute."""
    vc = member.guild.voice_client
    if not vc or before.channel != vc.channel or listeners(vc) > 0:
        return
    await asyncio.sleep(60)
    vc = member.guild.voice_client
    if vc and vc.is_connected() and listeners(vc) == 0:
        players.pop(member.guild.id, None)
        await vc.disconnect()


@client.event
async def on_ready():
    await tree.sync()
    log.info("Logged in as %s — slash commands synced.", client.user)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in a .env file next to bot.py")
    client.run(TOKEN, log_handler=log_handler, root_logger=True)
