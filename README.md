# PyXmasBot / PyNewYearBot

Python reimplementations of [xmasbot](https://github.com/TehPeGaSuS/xmasbot),
keeping the same behaviour with much less code. Shared logic lives in
`holidaybot.py`; `pyxmasbot.py` and `pynewyearbot.py` are thin scripts that
plug in a holiday name, command, and target date.

## What they do

Connect to an IRC server, join one or more channels, and announce a holiday
as it arrives in each UTC offset in order, from UTC+14 down to UTC-12. They
also answer commands in-channel:

- `!next` — time until the next announcement
- `!previous` / `!prev` — time since the last one
- `!remaining` — how many timezones are left this cycle
- `!xmas <location|TZ abbr|UTC offset>` (pyxmasbot) / `!newyear ...`
  (pynewyearbot) — holiday status for a place, e.g. `!xmas Tokyo`,
  `!xmas CET`, `!xmas UTC+9`
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
  auto-reconnect on disconnect. `--colors` turns on the same IRC bold/color
  formatting the original bot uses.
- **Scheduling**: an async loop walks the sorted timezone list, sleeping
  until each one's local midnight on the target date, broadcasting the
  holiday message, then rolling over to next year once all zones are done.

## Install

```
pip install -r requirements.txt
```

## Usage

```
python3 pyxmasbot.py --host irc.libera.chat --nick pyxmasbot \
    --channels '#test' --email you@example.com

python3 pynewyearbot.py --host irc.libera.chat --nick pynewyearbot \
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
| `--colors` | off | use IRC bold/color formatting in messages |
| `--password` | | IRC server password |
| `--sasl-nick` / `--sasl-pass` | | SASL PLAIN credentials |
| `--no-ssl` | | disable TLS |

### Multiple networks

Pass `--config networks.jsonc` instead of the flags above to run several
networks from one process. It's JSON with `//` and `/* */` comments allowed
(stripped before parsing — see `networks.example.jsonc`); each entry takes
the same fields as the CLI flags, with `host`/`nick`/`channels`/`email`
required and everything else optional:

```jsonc
[
  {
    "host": "irc.libera.chat",
    "nick": "pyxmasbot",
    "channels": ["#test"],
    "email": "you@example.com"
  }
]
```

### A note on Nominatim

The public `nominatim.openstreetmap.org` instance enforces a 1 req/sec limit
per its usage policy, and in practice throttles harder than that (heavy
scraping traffic industry-wide has made public instances more aggressive
about `429 Too Many Requests`). `!xmas <place>` and `!time <place>` requests
are rate-limited client-side and results are cached, but you may still hit
429s under the public instance. Point `--nominatim` at your own instance for
reliable service.
