# Discord Music Bot

Plays music in a voice channel from YouTube links, YouTube playlists, search terms, or links from
other sites yt-dlp supports (SoundCloud, Bandcamp, Vimeo, ...). When the queue runs out, autoplay keeps
going with similar songs and similar artists, using YouTube's Mix for the last song played.

## Commands
| Command | What it does |
|---|---|
| `/play <link or search>` | Joins your voice channel and plays or queues a song or a whole playlist (up to 100 songs) |
| `/skip` | Skips the current song |
| `/skipto <position>` | Jumps to a song in the queue |
| `/pause` / `/resume` | Pause / resume |
| `/nowplaying` | Shows the current song and its link |
| `/queue [page]` | Shows what's playing and what's next |
| `/remove <position>` | Removes a song from the queue |
| `/move <from> <to>` | Moves a song in the queue |
| `/shuffle` | Shuffles the queue |
| `/clear` | Clears upcoming songs |
| `/loop off\|song\|queue` | Repeats the current song or the whole queue |
| `/autoplay on\|off` | Keep playing similar songs when the queue ends (on by default) |
| `/volume <0-100>` | Sets volume |
| `/stop` | Clears the queue and leaves |

The bot leaves on its own a minute after everyone else has left the voice channel.

## Setup
1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token** → copy it.
3. Copy `.env.example` to `.env` and paste the token: `DISCORD_TOKEN=...`
4. **OAuth2 → URL Generator**: tick scopes `bot` + `applications.commands`, and bot permissions
   `Connect`, `Speak`, `Send Messages`. Open the generated URL to invite the bot to your server.
5. Install and run:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python bot.py
   ```

## Run automatically at login (Windows)
Starts the bot in the background each time you log in, and restarts it if it crashes.
Output goes to `bot.log`.
```
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
Start-ScheduledTask -TaskName MusicBot
```
To turn it off: `powershell -ExecutionPolicy Bypass -File .\remove-autostart.ps1`

ffmpeg is bundled through the `imageio-ffmpeg` package, so no separate install is needed.
Optional: installing [Deno](https://deno.com) (`winget install DenoLand.Deno`) makes YouTube extraction more reliable.
