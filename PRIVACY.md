# Musica Privacy Policy

**Effective date:** October 26, 2026 (updated to cover `M.` prefix commands and listening stats)

This policy explains what the Musica Discord bot ("Musica", "the bot"), run by GreedTV ("we", "us"), collects,
why, and how you can delete it. We keep as little as possible.

## What Musica does *not* collect
- **Your messages.** To support `M.` commands (like `M.play`), Discord delivers messages from channels Musica can
  see to the bot. Musica ignores every message that doesn't start with `M.` followed by one of its commands, and it
  never saves or logs message text. Slash commands and button presses work the same way as before.
- **Your voice.** Musica joins voice channels deafened. It doesn't listen to, record or receive anyone's audio.
- **Payment or contact details.** Musica is free and never asks for them.

## What Musica saves
Saved data is stored in a database file on the computer that runs the bot.

| Data | Why | How long |
|---|---|---|
| **Saved playlists:** your Discord user ID, each playlist's name and creation time, and its songs' titles, links and lengths | So `/playlist` can save and play your playlists | Until you delete them |
| **Server settings:** the server's ID, volume, autoplay on/off, DJ role ID, 24/7 on/off, and the voice and text channel IDs used for 24/7 mode | So each server keeps its settings after a restart | Until the bot is removed from the server |
| **Listening stats:** your user ID; how long you listened per server and per day; the songs you heard (title, link, guessed artist, play count, time); and how long you listened together with each other person in the same server | For `/profile` and the `/top` leaderboard | Until you use `/tracking off` or `/deletemydata` |
| **Profile decorations:** your user ID, and the color, bio and favorite song you choose in the `/editprofile` editor | To decorate your `/profile` | Until you change them, press **Reset everything** in the editor, or use `/deletemydata` |
| **Profile background:** a copy of the picture you upload in the `/editprofile` editor, shrunk to the card's size. The original file isn't kept | To draw it behind your `/profile` card | Until you press **Remove background** or **Reset everything** in the editor, or use `/deletemydata` |
| **Play history:** for each song played in a server: the server's ID, the song's title, link and artist, when it played, and the user ID of who added it | For `/history`, `/top` and your "songs requested" count | 60 days |

### How listening stats work
- While music plays, the bot checks every 15 seconds who is in its voice channel. Deafened people aren't counted.
  The bot never hears anyone. It only sees who is in the channel, which Discord shows to everyone there.
- **Only you can show your profile.** When you type `/profile` or `M.profile`, your profile card is posted in that
  channel. Nobody else can bring up your profile; if someone tries (like `M.profile @you`), the bot refuses.
- **What the card shows:** your stats, top songs and artists, the people you listened with most in that server,
  and your top 3 servers by name (a server the bot has left shows as "Unknown server"). Everyone in the channel
  can see it, so only post your profile where you're happy to show this.
- "Listened with" only counts and shows people from the same server.
- `/top` shows each server's most played songs and biggest listeners from the last 7 days to everyone in that
  server. People with tracking off aren't counted or shown.
- `/tracking off` stops tracking you and deletes your stats right away, including from other people's
  "listened with" lists. `/tracking on` starts again from zero.

## What Musica keeps only temporarily
These are kept in memory while the bot runs, and are gone when it restarts or leaves the voice channel:
- the queue, and the display names of the people who added each song;
- the recently played songs, so autoplay doesn't repeat them;
- skip votes.

## Technical logs
The bot writes a log file on its computer to find and fix problems. It can contain error messages, server names,
search text that failed to find a song, and links that failed to play. The log is limited to about 4 MB, and older
entries are deleted automatically.

## Who else gets data
We don't sell or share your data. To work, Musica sends some data to these services:
- **Discord**, which delivers your commands and the bot's replies ([Discord Privacy Policy](https://discord.com/privacy)).
- **YouTube and other music sites**: your search text and the links you play, so the bot can find and stream the
  audio. These requests come from the bot's computer, not from yours.
- **[lrclib.net](https://lrclib.net)**: the song's title and artist when you ask for lyrics.

Song thumbnails in the player card are loaded from YouTube's image servers (i.ytimg.com) by Discord.

## Deleting your data
- `/playlist delete` deletes one playlist.
- `/tracking off` deletes your listening stats and stops new ones from being collected.
- `/deletemydata` deletes all your saved playlists, listening stats, profile decorations and background picture,
  and removes your user ID from the play history. That's everything Musica stores about you.
- A server's settings are deleted when Musica is removed from that server.
- For anything else, open an issue at <https://github.com/GreedTV/music-bot/issues>.

## Children
You must meet Discord's minimum age to use Discord, and therefore Musica.

## Security
The database is on a personal computer and isn't encrypted. Please don't put private information in playlist
names.

## Changes to this policy
We may update this policy. The date at the top shows the latest version.

## Contact
Open an issue at <https://github.com/GreedTV/music-bot/issues>.
