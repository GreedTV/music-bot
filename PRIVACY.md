# Musica Privacy Policy

**Effective date:** October 26, 2026

This policy explains what the Musica Discord bot ("Musica", "the bot"), run by GreedTV ("we", "us"), collects,
why, and how you can delete it. We keep as little as possible.

## What Musica does *not* collect
- **Your messages.** Musica only receives the slash commands you use and the buttons you press. It can't read
  normal chat messages.
- **Your voice.** Musica joins voice channels deafened. It doesn't listen to, record or receive anyone's audio.
- **Payment or contact details.** Musica is free and never asks for them.

## What Musica saves
Saved data is stored in a database file on the computer that runs the bot.

| Data | Why | How long |
|---|---|---|
| **Saved playlists:** your Discord user ID, each playlist's name and creation time, and its songs' titles, links and lengths | So `/playlist` can save and play your playlists | Until you delete them |
| **Server settings:** the server's ID, volume, autoplay on/off, DJ role ID, 24/7 on/off, and the voice and text channel IDs used for 24/7 mode | So each server keeps its settings after a restart | Until the bot is removed from the server |

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
- `/deletemydata` deletes all your saved playlists. That's everything Musica stores about you.
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
