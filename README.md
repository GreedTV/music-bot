# Musica: Discord Music Bot

Plays music in a voice channel from YouTube links, YouTube playlists, search terms, or links from
other sites yt-dlp supports (SoundCloud, Bandcamp, Vimeo, ...). When the queue runs out, autoplay keeps
going with similar songs and similar artists, using YouTube's Mix for the last song played.

[Terms of Service](TERMS.md) · [Privacy Policy](PRIVACY.md)

## Player card
Each song posts a **Now playing** card with a progress bar, who asked for it and what's up next, plus buttons:
⏮️ Back · ⏸️ Pause/Resume · ⏭️ Skip · ⏹️ Stop · 🔁 Loop (off → song → queue) · 🔀 Shuffle · 📻 Autoplay ·
📜 Queue · 🎤 Lyrics · ♾️ 24/7 (server managers only).
Only people in the bot's voice channel can use them. `/nowplaying` brings the card back to the bottom of the chat.

## Who controls the music
- **Skipping:** the person who added the song, DJs and server managers skip instantly. Anyone else votes,
  and the song is skipped once half the listeners have voted.
- **DJ role** (`/dj set @role`): when set, only DJs and server managers can pause, stop, seek, change volume,
  effects, loop, shuffle or edit the queue. Everyone can still add songs, vote to skip, and remove their own songs.
  Without a DJ role, everyone can control the music. Someone alone with the bot always can.
- While music plays for others, someone in a different voice channel can't pull the bot away.

## Commands
| Command | What it does |
|---|---|
| `/play <link or search>` | Plays or queues a song or playlist (up to 100 songs). Suggestions appear as you type |
| `/skip` | Skips, or votes to skip |
| `/previous` | Goes back to the previous song |
| `/pause` / `/resume` | Pause / resume |
| `/seek <time>` | Jumps to a time, like `1:30` |
| `/filter <effect>` | Bass boost, nightcore, slowed + reverb, 8D, or off |
| `/volume <0-100>` | Sets the volume (remembered per server) |
| `/lyrics [song]` | Lyrics for the current song, or any song (from lrclib.net) |
| `/stop` | Clears the queue and leaves |
| `/queue [page]` / `/nowplaying` | Shows the queue / the player card |
| `/remove` / `/move` / `/skipto` | Edits the queue |
| `/shuffle` / `/clear` | Shuffles / empties the queue |
| `/loop off\|song\|queue` | Repeats the current song or the whole queue |
| `/autoplay on\|off` | Keep playing similar songs when the queue ends (remembered per server) |
| `/playlist save\|add\|play\|show\|list\|delete` | Your own saved playlists, usable in any server |
| `/profile` | Posts your profile card in the channel: listening time, songs heard/requested, top songs and artists, streak, badges, who you listen with, top servers. Only you can show yours: `M.profile @someone` gets a playful "that's private" reply. A server the bot has left shows as "Unknown server", and someone it can't look up as "Unknown user" |
| `/editprofile` | Opens the profile editor: a live preview of your card with a 🎨 color menu (or a custom `#hex`), and buttons for 💬 Bio (100 characters, no links), ⭐ Favorite song (shown as typed), 🖼️ Background (upload a picture in the form), 🗑️ Remove background, 🧽 Reset everything and ✅ Done. Only you can use your editor. Badges are earned automatically |
| `/tracking on\|off` | Turn your listening stats on or off (off deletes them) |
| `/top` | This server's most played songs and biggest listeners in the last 7 days |
| `/history` | The last 20 songs played in this server |
| `/grab` | Sends the current song to your DMs |
| `/dj set\|clear` | Sets the DJ role (server managers) |
| `/24-7 on\|off` | Stays in the voice channel even when it's empty, and rejoins after a restart (server managers) |
| `/help` | Lists all commands |
| `/deletemydata` | Deletes everything the bot saved about you |

### Prefix commands
Every command also works with the `M.` prefix (any case), with the same options in order:
`M.play daft punk`, `M.move 3 1`, `M.loop song`, `M.playlist save chill`, `M.dj set @DJ`.

| Short | Command | | Short | Command |
|---|---|---|---|---|
| `M.p` | play | | `M.l` | loop |
| `M.s` / `M.next` | skip | | `M.ap` | autoplay |
| `M.back` / `M.prev` | previous | | `M.ly` | lyrics |
| `M.np` | nowplaying | | `M.f` | filter |
| `M.q` | queue | | `M.sh` | shuffle |
| `M.vol` / `M.v` | volume | | `M.rm` | remove |
| `M.dc` / `M.leave` | stop | | `M.pl` | playlist |
| `M.247` | 24-7 | | `M.h` | help |
| `M.pf` / `M.me` | profile | | `M.g` | grab |
| `M.hist` | history | | | |

The bot's status shows **Type /help or M.help**.

Without 24/7 mode, the bot leaves a minute after everyone else has left the voice channel.
Saved playlists, server settings and listening stats are stored in `data/musicbot.db` (SQLite).
`/profile` is drawn as a picture (`card.py`, using Pillow). Uploaded backgrounds are shrunk to the card's size
(1100×680, about 150–200 KB) and kept in `data/backgrounds/`.
Listening is counted every 15 seconds for everyone in the bot's voice channel who isn't deafened.

## Setup
1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. Keep **Public Bot** on and **Requires OAuth2 Code Grant** off.
   Under **Privileged Gateway Intents**, turn on **Message Content Intent** (needed for `M.` commands; the bot
   won't connect without it).
3. Copy `.env.example` to `.env` and paste the token: `DISCORD_TOKEN=...`
4. **Installation** tab: untick **User Install**. Under **Guild Install**, add scopes `applications.commands` and
   `bot`, and permissions `View Channels`, `Send Messages`, `Embed Links`, `Connect`, `Speak`.
   Use the install link (or **Add App** on the bot's profile) to add it to a server.
5. **General Information** tab: set **Terms of Service URL** and **Privacy Policy URL** to the links of
   [TERMS.md](TERMS.md) and [PRIVACY.md](PRIVACY.md) on GitHub.
6. Install and run:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python bot.py
   ```

## Run automatically at login (Windows)
Starts the bot in the background each time you log in, and restarts it if it crashes.
Output goes to `bot.log`. While music is playing, the bot also keeps Windows from going to sleep.
```
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
Start-ScheduledTask -TaskName MusicBot
```
To turn it off: `powershell -ExecutionPolicy Bypass -File .\remove-autostart.ps1`

ffmpeg is bundled through the `imageio-ffmpeg` package, so no separate install is needed.
Optional: installing [Deno](https://deno.com) (`winget install DenoLand.Deno`) makes YouTube extraction more reliable.
