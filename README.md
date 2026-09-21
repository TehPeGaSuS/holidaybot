# PyXmasBot

A single-file Python reimplementation of [xmasbot](https://github.com/TehPeGaSuS/xmasbot),
keeping the same behaviour with much less code (~360 lines, one file, stdlib
`asyncio` for IRC instead of a full IRC library).

## Same functionality

- Announces Christmas as it happens in each UTC offset, in order.
- `!next`, `!previous`, `!remaining`, `!xmas <location|TZ abbr|UTC offset>`,
  `!time <location|TZ abbr|UTC offset>`, `!time`, `!help`, `!source`.
- Nominatim geocoding for free-text locations, with an in-memory cache.

## Deliberately simplified

- **One IRC network per process.** The original supported a YAML list of
  networks/bots in one binary; here, run a second `pyxmasbot.py` process if
  you need a second network — simpler than a config format for a case most
  people don't use.
- **No embedded CSV + code-generation pipeline.** The original builds its
  timezone-abbreviation table (`UTC`, `PST`, `CEST`, ...) from a bundled
  `time_zone.csv` via a `go generate` step and a handful of `utils/*` helper
  programs. This version builds the same kind of table at startup from the
  system's IANA tz database (`zoneinfo.available_timezones()`), so there's
  nothing to regenerate or ship.
- **No YAML config / multi-network validation layer.** CLI flags only.
- Colors, flood-limit tuning, and bind-address options were dropped since
  they're rarely used; flood protection is a fixed ~2 msg/s cap instead.

`tz.json` (the country/city list per UTC offset) is reused as-is from the
original project.

## Verified against a real IRC network

Tested live against `irc.ptirc.org` (TLS) in `##testchan`: TLS handshake,
registration, channel join, and every command (`!help`, `!source`, `!next`,
`!previous`, `!remaining`, `!time`, `!xmas`) including Nominatim geocoding
edge cases (state-level disambiguation, postal-code-only address components,
"no such place"). Two bugs surfaced and were fixed during that testing:

- `!previous`'s UTC-12 year-rollover math was wrong (compared against the
  wrong year's target date).
- The short-address formatter could pick a postal code instead of a
  state/region for countries whose Nominatim hierarchy puts it there; it now
  skips numeric-only components.

Not exercised: SASL auth (no test credentials available) and a full
multi-hour run of the announce loop hitting a real timezone boundary.

## Usage

```
pip install -r requirements.txt
python3 pyxmasbot.py --host irc.libera.chat --nick pyxmasbot \
    --channels '#test' --email you@example.com
```

Flags: `--port` (6697), `--no-ssl`, `--password`, `--sasl-nick`/`--sasl-pass`,
`--prefix` (`!`), `--nominatim` (defaults to the public instance).
