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

Free-text locations are resolved with the [LocationIQ](https://locationiq.com/)
geocoding API (Nominatim-compatible, but with a usable free-tier rate limit),
using its `/search` endpoint for geocoding and its `/timezone` endpoint for
coordinate → IANA timezone lookup. Get a free API key at
[locationiq.com](https://locationiq.com/) (free tier: 5,000 req/day, 2 req/sec).

## How it works

- **Timezones**: `tz.json` (reused from the original project) lists the
  countries/cities per UTC offset, used for the scheduled announcements.
  Timezone-abbreviation lookups (`UTC`, `CET`, `PST`, ...) are built at
  startup directly from the standard library's IANA tz database
  (`zoneinfo.available_timezones()`) rather than a bundled data file.
- **IRC**: a small hand-rolled `asyncio` client — connect over TLS, register,
  join channels, respond to PING, handle PRIVMSG, basic SASL PLAIN, and
  auto-reconnect on disconnect. IRC bold/color formatting (`--colors`,
  `--no-colors` to disable) is on by default.
- **Scheduling**: an async loop walks the sorted timezone list, sleeping
  until each one's local midnight on the target date, broadcasting the
  holiday message, then rolling over to next year once all zones are done.

## Install

```
pip install -r requirements.txt
```

## Usage

```
export LOCATIONIQ_API_KEY=your-locationiq-api-key

python3 pyxmasbot.py --host irc.libera.chat --nick pyxmasbot --channels '#test'

python3 pynewyearbot.py --host irc.libera.chat --nick pynewyearbot --channels '#test'
```

`$LOCATIONIQ_API_KEY` is picked up automatically (`--api-key` overrides it if
both are set), so one API key covers every network without repeating it. A
`.env` file (`LOCATIONIQ_API_KEY=...`, one `KEY=VALUE` per line) in the
working directory is loaded automatically too, without overriding a variable
that's already set in the real environment.

| Flag | Default | Description |
|---|---|---|
| `--host` | *(required)* | IRC server hostname |
| `--port` | `6697` | IRC server port |
| `--nick` | *(required)* | bot nickname |
| `--channels` | *(required)* | one or more channels, e.g. `--channels '#test' '#test2'` |
| `--api-key` | `$LOCATIONIQ_API_KEY` | LocationIQ API key |
| `--prefix` | `!` | command prefix |
| `--geocoder-url` | `https://us1.locationiq.com/v1` | LocationIQ (or Nominatim-compatible) server |
| `--colors` / `--no-colors` | on | use IRC bold/color formatting in messages |
| `--password` | | IRC server password |
| `--sasl-nick` / `--sasl-pass` | | SASL PLAIN credentials |
| `--no-ssl` | | disable TLS |
| `--bind` | | local IPv4/IPv6 address to bind the outgoing connection to |

### Multiple networks

Pass `--config networks.jsonc` instead of the flags above to run several
networks from one process. It's JSON with `//` and `/* */` comments allowed
(stripped before parsing — see `networks.example.jsonc`). Each network takes
the same fields as the CLI flags, with only `host`/`nick`/`channels`
required. Put settings shared across every network — typically `api_key` —
at the top level alongside `networks`; a network can still override a shared
field for itself:

```jsonc
{
  "api_key": "your-locationiq-api-key",
  "networks": [
    {
      "host": "irc.libera.chat",
      "nick": "pyxmasbot",
      "channels": ["#test"]
    }
  ]
}
```

A plain `[{...}, {...}]` list (no shared settings, `api_key` per entry or via
`$LOCATIONIQ_API_KEY`/`.env`) still works too.
