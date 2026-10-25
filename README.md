# Discord Music Bot

Plays audio from YouTube links or search terms in a voice channel.

## Commands
| Command | What it does |
|---|---|
| `/play <url or search>` | Joins your voice channel and plays (or queues) a song |
| `/skip` | Skips the current song |
| `/pause` / `/resume` | Pause / resume |
| `/queue` | Shows what's playing and what's next |
| `/volume <0-100>` | Sets volume |
| `/stop` | Clears the queue and leaves |

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

ffmpeg is bundled through the `imageio-ffmpeg` package, so no separate install is needed.
Optional: installing [Deno](https://deno.com) (`winget install DenoLand.Deno`) makes YouTube extraction more reliable.
