"""Discord music bot: plays YouTube (and other sites) by link or search, with a queue and autoplay."""
import asyncio
import ctypes
import logging
import logging.handlers
import os
import random
import sys
import time
from collections import deque
from pathlib import Path
from typing import Literal

import discord
import imageio_ffmpeg
import yt_dlp
from discord import app_commands
from discord.ext import tasks
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
                 stream_url: str | None = None, duration: float | None = None):
        self.title = title
        self.url = url  # page URL, used to (re)resolve the audio stream
        self.requester = requester
        self.youtube_id = youtube_id
        self.stream_url = stream_url
        self.resolved_at = time.time() if stream_url else 0.0
        self.duration = duration

    @property
    def thumbnail(self) -> str | None:
        return f"https://i.ytimg.com/vi/{self.youtube_id}/hqdefault.jpg" if self.youtube_id else None


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "Live"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


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
        self.np_message: discord.Message | None = None  # the "Now playing" card with the buttons


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
    return Track(entry.get("title") or url, url, requester, yt_id, duration=entry.get("duration"))


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
    track = Track(info["title"], info.get("webpage_url", query), requester, _youtube_id(info), info.get("url"),
                  info.get("duration"))
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
        await post_now_playing(player)


def is_active(vc: discord.VoiceClient | None) -> bool:
    return bool(vc and (vc.is_playing() or vc.is_paused()))


# ---- "Now playing" card with control buttons ----

def now_playing_embed(player: GuildPlayer, paused: bool = False) -> discord.Embed:
    track = player.current
    autoplayed = track.requester == "Autoplay"
    embed = discord.Embed(title=track.title[:256], url=track.url if str(track.url).startswith("http") else None,
                          color=0xFAA61A if paused else 0x5865F2)
    embed.set_author(name="⏸️ Paused" if paused else "🎵 Now playing")
    embed.add_field(name="Duration", value=format_duration(track.duration))
    embed.add_field(name="Requested by", value="🔁 Autoplay (similar song)" if autoplayed else track.requester)
    if player.queue:
        up_next = player.queue[0].title
    elif player.loop != "off":
        up_next = "This song again (loop)"
    else:
        up_next = "Autoplay will pick a similar song" if player.autoplay else "Nothing. Add songs with /play"
    embed.add_field(name="Up next", value=up_next[:1024], inline=False)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.set_footer(text=f"Loop: {player.loop} · Autoplay: {'on' if player.autoplay else 'off'} · "
                          f"Volume: {round(player.volume * 100)}% · {len(player.queue)} in queue")
    return embed


class PlayerControls(discord.ui.View):
    """Buttons under the "Now playing" card. Persistent: custom_ids keep them working after a restart."""

    def __init__(self, player: GuildPlayer | None = None, paused: bool = False):
        super().__init__(timeout=None)
        if paused:
            self.pause_resume.label, self.pause_resume.emoji = "Resume", "▶️"
        if player and player.loop != "off":
            self.loop_mode.label = f"Loop: {player.loop}"
            self.loop_mode.style = discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected() or interaction.guild.id not in players:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("Join my voice channel to use the controls.", ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction):
        player = get_player(interaction.guild.id)
        paused = interaction.guild.voice_client.is_paused()
        if player.current:
            await interaction.response.edit_message(embed=now_playing_embed(player, paused),
                                                    view=PlayerControls(player, paused))
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="musicbot:pause", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        await self._redraw(interaction)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="musicbot:skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        player = get_player(interaction.guild.id)
        if not is_active(vc):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        title = player.current.title if player.current else "the song"
        await interaction.response.send_message(f"⏭️ **{interaction.user.display_name}** skipped **{title}**.")
        player.skipping = True
        vc.stop()

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="musicbot:stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"⏹️ **{interaction.user.display_name}** stopped the music.")
        await stop_playback(interaction.guild)

    @discord.ui.button(label="Loop: off", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="musicbot:loop", row=0)
    async def loop_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild.id)
        player.loop = {"off": "song", "song": "queue", "queue": "off"}[player.loop]
        await self._redraw(interaction)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="musicbot:shuffle", row=0)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild.id)
        if not shuffle_queue(player):
            return await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
        await self._redraw(interaction)
        await interaction.followup.send(f"🔀 **{interaction.user.display_name}** shuffled the queue.")

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="musicbot:queue", row=1)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(queue_text(get_player(interaction.guild.id)), ephemeral=True)


async def post_now_playing(player: GuildPlayer):
    if not player.text_channel or not player.current:
        return
    try:
        player.np_message = await player.text_channel.send(embed=now_playing_embed(player), view=PlayerControls(player))
    except discord.HTTPException:
        log.warning("Could not send the now-playing card")


async def refresh_now_playing(guild: discord.Guild):
    """Redraw the card after a slash command changed something it shows."""
    player = players.get(guild.id)
    if not player or not player.np_message or not player.current:
        return
    paused = bool(guild.voice_client and guild.voice_client.is_paused())
    try:
        await player.np_message.edit(embed=now_playing_embed(player, paused), view=PlayerControls(player, paused))
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


async def stop_playback(guild: discord.Guild):
    player = players.pop(guild.id, None)
    if player:
        await retire_now_playing(player)
    if guild.voice_client:
        await guild.voice_client.disconnect()


def shuffle_queue(player: GuildPlayer) -> bool:
    if len(player.queue) < 2:
        return False
    items = list(player.queue)
    random.shuffle(items)
    player.queue = deque(items)
    return True


def queue_text(player: GuildPlayer, page: int = 1) -> str:
    status = f"Loop: **{player.loop}** · Autoplay: **{'on' if player.autoplay else 'off'}**"
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
    if is_active(vc):
        await refresh_now_playing(interaction.guild)
    else:
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
        await interaction.response.send_message("⏸️ Paused.")
        return await refresh_now_playing(interaction.guild)
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")
        return await refresh_now_playing(interaction.guild)
    await interaction.response.send_message("Nothing is paused.", ephemeral=True)


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
    paused = interaction.guild.voice_client.is_paused()
    await interaction.response.send_message(embed=now_playing_embed(player, paused), view=PlayerControls(player, paused))
    player.np_message = await interaction.original_response()


@tree.command(description="Remove a song from the queue")
@app_commands.describe(position="Position in /queue")
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    player = get_player(interaction.guild.id)
    if position > len(player.queue):
        return await interaction.response.send_message("There's no song at that position.", ephemeral=True)
    track = player.queue[position - 1]
    del player.queue[position - 1]
    await interaction.response.send_message(f"Removed **{track.title}**.")
    await refresh_now_playing(interaction.guild)


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
    await refresh_now_playing(interaction.guild)


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
    if not shuffle_queue(get_player(interaction.guild.id)):
        return await interaction.response.send_message("Not enough songs to shuffle.", ephemeral=True)
    await interaction.response.send_message("🔀 Shuffled the queue.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Clear upcoming songs (keeps the current one playing)")
async def clear(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    player.queue.clear()
    await interaction.response.send_message("🧹 Cleared the queue.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Repeat the current song, the whole queue, or nothing")
async def loop(interaction: discord.Interaction, mode: Literal["off", "song", "queue"]):
    get_player(interaction.guild.id).loop = mode
    await interaction.response.send_message(f"🔁 Loop set to **{mode}**.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="When the queue ends, keep playing similar songs")
async def autoplay(interaction: discord.Interaction, mode: Literal["on", "off"]):
    get_player(interaction.guild.id).autoplay = mode == "on"
    await interaction.response.send_message(f"Autoplay is now **{mode}**.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Set volume (0-100)")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
    player = get_player(interaction.guild.id)
    player.volume = percent / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to {percent}%.")
    await refresh_now_playing(interaction.guild)


@tree.command(description="Stop, clear the queue, and leave the voice channel")
async def stop(interaction: discord.Interaction):
    if not interaction.guild.voice_client:
        return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
    await interaction.response.send_message("⏹️ Stopped and left the channel.")
    await stop_playback(interaction.guild)


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Leave when everyone else has left the voice channel for a minute."""
    vc = member.guild.voice_client
    if not vc or before.channel != vc.channel or listeners(vc) > 0:
        return
    await asyncio.sleep(60)
    vc = member.guild.voice_client
    if vc and vc.is_connected() and listeners(vc) == 0:
        await stop_playback(member.guild)


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


@tasks.loop(seconds=30)
async def keep_awake_while_playing():
    """On Windows, stop the PC from going to sleep while music is playing (like a video player does)."""
    playing = any(vc.is_playing() for vc in client.voice_clients)
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if playing else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


@client.event
async def on_ready():
    if sys.platform == "win32" and not keep_awake_while_playing.is_running():
        keep_awake_while_playing.start()
    client.add_view(PlayerControls())  # makes buttons on existing cards work again after a restart
    await tree.sync()
    log.info("Logged in as %s — slash commands synced.", client.user)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in a .env file next to bot.py")
    client.run(TOKEN, log_handler=log_handler, root_logger=True)
