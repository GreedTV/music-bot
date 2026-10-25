"""Simple Discord music bot: plays audio from YouTube links or search terms."""
import asyncio
import os
from collections import deque

import discord
import imageio_ffmpeg
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
}


class Track:
    def __init__(self, title: str, stream_url: str, page_url: str, requester: str):
        self.title = title
        self.stream_url = stream_url
        self.page_url = page_url
        self.requester = requester


class GuildPlayer:
    """Per-server queue and playback state."""

    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
players: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    return players.setdefault(guild_id, GuildPlayer())


async def search(query: str, requester: str) -> Track:
    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:  # search result
                info = info["entries"][0]
            return info

    info = await asyncio.to_thread(_extract)
    return Track(info["title"], info["url"], info.get("webpage_url", query), requester)


def play_next(guild: discord.Guild):
    player = get_player(guild.id)
    vc = guild.voice_client
    if not vc or not player.queue:
        player.current = None
        return
    player.current = player.queue.popleft()
    source = discord.FFmpegPCMAudio(player.current.stream_url, executable=FFMPEG_EXE, **FFMPEG_OPTS)
    source = discord.PCMVolumeTransformer(source, volume=0.5)
    vc.play(source, after=lambda err: client.loop.call_soon_threadsafe(play_next, guild))


@tree.command(description="Play a song from a YouTube link or search terms")
@app_commands.describe(query="YouTube URL or search terms")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)

    await interaction.response.defer()
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    try:
        track = await search(query, interaction.user.display_name)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't find that: `{e}`")

    player = get_player(interaction.guild.id)
    player.queue.append(track)
    if vc.is_playing() or vc.is_paused():
        await interaction.followup.send(f"Queued **{track.title}** (position {len(player.queue)})")
    else:
        play_next(interaction.guild)
        await interaction.followup.send(f"Now playing **{track.title}**\n{track.page_url}")


@tree.command(description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    vc.stop()  # triggers the after-callback, which plays the next track
    await interaction.response.send_message("Skipped.")


@tree.command(description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message("Paused.")
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message("Resumed.")
    await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@tree.command(description="Show the queue")
async def queue(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if not player.current and not player.queue:
        return await interaction.response.send_message("The queue is empty.")
    lines = []
    if player.current:
        lines.append(f"**Now playing:** {player.current.title} (requested by {player.current.requester})")
    for i, t in enumerate(list(player.queue)[:10], start=1):
        lines.append(f"`{i}.` {t.title} (requested by {t.requester})")
    if len(player.queue) > 10:
        lines.append(f"...and {len(player.queue) - 10} more")
    await interaction.response.send_message("\n".join(lines))


@tree.command(description="Set volume (0-100)")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
    vc = interaction.guild.voice_client
    if not vc or not isinstance(vc.source, discord.PCMVolumeTransformer):
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    vc.source.volume = percent / 100
    await interaction.response.send_message(f"Volume set to {percent}%.")


@tree.command(description="Stop, clear the queue, and leave the voice channel")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    player = get_player(interaction.guild.id)
    player.queue.clear()
    player.current = None
    if vc:
        await vc.disconnect()
        return await interaction.response.send_message("Stopped and left the channel.")
    await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user} — slash commands synced.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in a .env file next to bot.py")
    client.run(TOKEN)
