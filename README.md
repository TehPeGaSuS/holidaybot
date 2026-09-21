# PyXmasBot

A single-file Python reimplementation of [xmasbot](https://github.com/TehPeGaSuS/xmasbot),
keeping the same behaviour with much less code (~360 lines, one file, stdlib
`asyncio` for IRC instead of a full IRC library).

## What it does

Connects to an IRC server, joins one or more channels, and announces Merry
Christmas as it arrives in each UTC offset in order, from UTC+14 down to
UTC-12. It also answers commands in-channel:

- `!next` — time until the next Christmas announcement
- `!previous` / `!prev` — time since the last one
- `!remaining` — how many timezones are left this year
- `!xmas <location|TZ abbr|UTC offset>` — Christmas status for a place
  (e.g. `!xmas Tokyo`, `!xmas CET`, `!xmas UTC+9`)
- `!time <location|TZ abbr|UTC offset>` — current time for a place
- `!time` — current UTC time
- `!help` — list commands
- `!source` — link to this repo

Free-text locations are resolved with the [Nominatim](https://nominatim.org/)
geocoding API plus [`timezonefinder`](https://github.com/jannikmi/timezonefinder)
for coordinate → IANA timezone lookup.

## How it works

- **Timezones**: `tz.json` (reused from the original project) lists the
  countries/cities per UTC offset, used for the scheduled announcements.
  Timezone-abbreviation lookups (`UTC`, `CET`, `PST`, ...) are built at
  startup directly from the standard library's IANA tz database
  (`zoneinfo.available_timezones()`) rather than a bundled data file.
- **IRC**: a small hand-rolled `asyncio` client — connect over TLS, register,
  join channels, respond to PING, handle PRIVMSG, basic SASL PLAIN, and
  auto-reconnect on disconnect.
- **Scheduling**: an async loop walks the sorted timezone list, sleeping
  until each one's local midnight on Dec 25, broadcasting a "Merry
  Christmas" message, then rolling over to next year once all zones are done.

## Install

```
pip install -r requirements.txt
```

## Usage

```
python3 pyxmasbot.py --host irc.libera.chat --nick pyxmasbot \
    --channels '#test' --email you@example.com
```

| Flag | Default | Description |
|---|---|---|
| `--host` | *(required)* | IRC server hostname |
| `--port` | `6697` | IRC server port |
| `--nick` | *(required)* | bot nickname |
| `--channels` | *(required)* | one or more channels, e.g. `--channels '#test' '#test2'` |
| `--email` | *(required)* | contact email sent to Nominatim |
| `--prefix` | `!` | command prefix |
| `--nominatim` | `https://nominatim.openstreetmap.org` | Nominatim server |
| `--password` | | IRC server password |
| `--sasl-nick` / `--sasl-pass` | | SASL PLAIN credentials |
| `--no-ssl` | | disable TLS |

Need a second IRC network? Run a second `pyxmasbot.py` process with its own
flags — there's no built-in multi-network config.
