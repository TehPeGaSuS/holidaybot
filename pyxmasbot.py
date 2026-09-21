#!/usr/bin/env python3
"""PyXmasBot: a Merry Christmas IRC bot — announces Christmas as it arrives in each UTC offset.

A from-scratch, single-file reimplementation of https://github.com/TehPeGaSuS/xmasbot
(the original Go bot) keeping the same user-facing behaviour (commands,
announcements) with far less code:
one IRC connection, no YAML/multi-network config, no code-gen pipeline for the
timezone-abbreviation table (built at runtime from the stdlib's zoneinfo database
instead of a bundled CSV + generator).

Commands (prefix configurable, default "!"):
    !next                 time until the next Christmas announcement
    !previous / !prev      time since the last Christmas announcement
    !remaining             how many timezones are left this year
    !xmas <place|TZ|UTC+N> Christmas status for a location, tz abbreviation, or UTC offset
    !time <place|TZ|UTC+N> current time for a location, tz abbreviation, or UTC offset
    !time                  current UTC time
    !help                  list commands
    !source                link to this bot's source
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import available_timezones, ZoneInfo

try:
    import requests
except ImportError:
    requests = None

try:
    from timezonefinder import TimezoneFinder
except ImportError:
    TimezoneFinder = None

HERE = Path(__file__).resolve().parent
SOURCE_URL = "https://github.com/TehPeGaSuS/pyxmasbot"
ORIGINAL_URL = "https://github.com/TehPeGaSuS/xmasbot"  # the Go bot this ports


# ---------------------------------------------------------------------------
# Timezone data: reuse the original project's country/city list (tz.json),
# plus a runtime-built abbreviation table (replaces the CSV + codegen tools).
# ---------------------------------------------------------------------------

@dataclass
class TZEntry:
    offset: float  # hours from UTC
    countries: list[dict]

    def label(self) -> str:
        parts = []
        for c in self.countries:
            cities = c.get("cities") or []
            parts.append(f"{c['name']} ({', '.join(cities)})" if cities else c["name"])
        return ", ".join(parts)


def strip_json_comments(text: str) -> str:
    """Strip // and /* */ comments from JSONC-ish text, respecting strings."""
    out = []
    i, n, in_string = 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i = text.find("\n", i)
            i = n if i == -1 else i
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(path: str):
    with open(path) as f:
        return json.loads(strip_json_comments(f.read()))


def load_zones() -> list[TZEntry]:
    data = json.loads((HERE / "tz.json").read_text())
    zones = [TZEntry(z["offset"], z["countries"]) for z in data]
    zones.sort(key=lambda z: z.offset, reverse=True)  # earliest Christmas first
    return zones


def build_abbr_table() -> dict[str, int]:
    """abbr -> current UTC offset in seconds, derived from the system tz database."""
    table: dict[str, int] = {}
    now = datetime.now(timezone.utc)
    for name in available_timezones():
        try:
            local = now.astimezone(ZoneInfo(name))
            abbr = local.tzname()
            if not abbr or not abbr.isalpha():
                continue
            table.setdefault(abbr.upper(), int(local.utcoffset().total_seconds()))
        except Exception:
            continue
    return table


_UTC_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


class TZParseError(ValueError):
    pass


def parse_utc_offset(text: str) -> int:
    """Parse 'UTC+5', 'GMT-3:30', '+05:00' -> offset in seconds."""
    m = _UTC_RE.match(text.strip())
    if not m:
        raise TZParseError(f"couldn't parse timezone: {text!r}")
    sign, hours, minutes = m.groups()
    hours, minutes = int(hours), int(minutes or 0)
    if hours > 14 or (hours == 14 and minutes > 0):
        raise TZParseError("timezone offset out of range")
    total = hours * 3600 + minutes * 60
    return -total if sign == "-" else total


def resolve_offset(query: str, abbrs: dict[str, int]) -> int:
    """Resolve a tz abbreviation or a UTC offset string to seconds."""
    key = query.strip().upper()
    if key in abbrs:
        return abbrs[key]
    return parse_utc_offset(query)


def human_duration(delta: timedelta) -> str:
    total = int(abs(delta.total_seconds()))
    weeks, rem = divmod(total, 7 * 86400)
    days, rem = divmod(rem, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    units = [("week", weeks), ("day", days), ("hour", hours), ("minute", minutes), ("second", seconds)]
    parts = [(n, v) for n, v in units if v > 0]
    parts = parts[:2] or [("second", 0)]
    return " ".join(f"{v} {n}{'s' if v != 1 else ''}" for n, v in parts)


def short_address(display_name: str) -> str:
    """Nominatim's display_name is a full admin hierarchy (city, county,
    region, historical region, postal code, country, ...). Keep city +
    state/region + country: enough to tell apart e.g. Paris, France vs.
    Paris, Texas, United States, without the verbose county/historical-region
    middle. Postal codes (or any numeric-only component) are skipped, since
    they disambiguate nothing and some countries put them right before the
    country name."""
    parts = [p.strip() for p in display_name.split(",")]
    country = parts[-1]
    rest = [p for p in parts[:-1] if not p.replace(" ", "").isdigit()]
    if not rest:
        return display_name
    if len(rest) == 1:
        return f"{rest[0]}, {country}"
    return f"{rest[0]}, {rest[-1]}, {country}"


def normalize(s: str) -> str:
    return " ".join(s.strip().lower().split())


def next_target(now: datetime) -> datetime:
    """This year's Dec 25 00:00 UTC while we're still before it (through Dec 25
    AoE), otherwise next year's."""
    year = now.year if (now.month, now.day) < (12, 26) else now.year + 1
    return datetime(year, 12, 25, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Nominatim geocoding (same free service as the original bot), with a tiny
# in-memory cache and offline coordinate -> IANA tz lookup via timezonefinder.
# ---------------------------------------------------------------------------

class Geocoder:
    def __init__(self, email: str, server: str):
        self.email = email
        self.server = server.rstrip("/")
        self.cache: dict[str, dict | None] = {}
        self.finder = TimezoneFinder() if TimezoneFinder else None

    def lookup(self, place: str) -> dict | None:
        if place in self.cache:
            return self.cache[place]
        if requests is None:
            raise RuntimeError("the 'requests' package is required for location lookups")
        resp = requests.get(
            f"{self.server}/search",
            params={"q": place, "format": "json", "accept-language": "en", "limit": 1, "email": self.email},
            headers={"User-Agent": f"pyxmasbot: {SOURCE_URL}"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        result = results[0] if results else None
        self.cache[place] = result
        return result

    def tz_for(self, place: str) -> tuple[ZoneInfo, str] | None:
        result = self.lookup(place)
        if not result:
            return None
        if self.finder is None:
            raise RuntimeError("the 'timezonefinder' package is required for location lookups")
        tzid = self.finder.timezone_at(lat=float(result["lat"]), lng=float(result["lon"]))
        if not tzid:
            return None
        return ZoneInfo(tzid), short_address(result["display_name"])


# ---------------------------------------------------------------------------
# Minimal asyncio IRC client (single server/network — the original supported
# a YAML list of networks; if you need more than one, run another process).
# ---------------------------------------------------------------------------

class IRC:
    def __init__(self, host, port, nick, channels, ssl_on=True, password=None,
                 sasl_user=None, sasl_pass=None):
        self.host, self.port, self.nick = host, port, nick
        self.channels = channels
        self.ssl_on = ssl_on
        self.password = password
        self.sasl_user, self.sasl_pass = sasl_user, sasl_pass
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._last_send = 0.0
        self.joined = asyncio.Event()  # set once we've sent JOIN for all channels

    async def connect(self):
        ctx = ssl.create_default_context() if self.ssl_on else None
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port, ssl=ctx)
        if self.sasl_pass:
            self._raw("CAP REQ :sasl")
        if self.password:
            self._raw(f"PASS {self.password}")
        self._raw(f"NICK {self.nick}")
        self._raw(f"USER {self.nick} 0 * :{self.nick}")

    def _raw(self, line: str):
        self.writer.write((line + "\r\n").encode("utf-8", "replace"))

    async def _send(self, line: str):
        # basic flood protection: at most ~1 line per 0.5s
        now = time.monotonic()
        wait = 0.5 - (now - self._last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        self._raw(line)
        await self.writer.drain()
        self._last_send = time.monotonic()

    async def privmsg(self, target: str, text: str):
        for line in text.split("\n"):
            await self._send(f"PRIVMSG {target} :{line}")

    async def run(self, on_message):
        await self.connect()
        while True:
            line = await self.reader.readline()
            if not line:
                raise ConnectionError("disconnected")
            msg = line.decode("utf-8", "replace").rstrip("\r\n")
            if msg.startswith("PING"):
                self._raw("PONG" + msg[4:])
                continue
            if "CAP" in msg and "sasl" in msg and self.sasl_pass:
                self._raw("AUTHENTICATE PLAIN")
                continue
            if msg.startswith("AUTHENTICATE"):
                import base64
                token = f"{self.sasl_user}\0{self.sasl_user}\0{self.sasl_pass}"
                self._raw("AUTHENTICATE " + base64.b64encode(token.encode()).decode())
                continue
            if " 903 " in msg or " 904 " in msg:  # SASL success/fail
                self._raw("CAP END")
                continue
            if " 001 " in msg:  # RPL_WELCOME: registration complete, safe to join/send
                for ch in self.channels:
                    self._raw(f"JOIN {ch}")
                self.joined.set()
            if "PRIVMSG" in msg:
                m = re.match(r":(\S+)!\S+ PRIVMSG (\S+) :(.*)", msg)
                if m:
                    sender, target, text = m.groups()
                    reply_to = target if target.startswith("#") else sender.split("!")[0]
                    await on_message(sender, reply_to, text)


# ---------------------------------------------------------------------------
# Bot logic
# ---------------------------------------------------------------------------

HELP_MSG = "Commands: '{p}xmas <location>', '{p}time <location>', '{p}next', '{p}previous', '{p}remaining', '{p}help', '{p}source'"


@dataclass
class Bot:
    irc: IRC
    prefix: str
    zones: list[TZEntry]
    abbrs: dict[str, int]
    geocoder: Geocoder
    target: datetime = field(default_factory=lambda: next_target(datetime.now(timezone.utc)))
    index: int = 0
    announced_first: bool = False

    def zone_offset_seconds(self, z: TZEntry) -> int:
        return int(z.offset * 3600)

    async def announce_loop(self):
        await self.irc.joined.wait()
        while True:
            for i, z in enumerate(self.zones):
                self.index = i
                dur = timedelta(seconds=self.zone_offset_seconds(z))
                now = datetime.now(timezone.utc)
                if now + dur >= self.target:
                    continue  # already passed this run (e.g. bot restarted mid-way)
                wait = (self.target - dur - now).total_seconds()
                hdur = human_duration(timedelta(seconds=wait))
                label = "First Merry Christmas" if i == 0 else (
                    "Final Merry Christmas" if i == len(self.zones) - 1 else "Next Merry Christmas")
                await self.broadcast(f"{label} in {hdur} in {z.label()}")
                await asyncio.sleep(max(wait, 0))
                await self.broadcast(f"Merry Christmas in {z.label()}")
            await self.broadcast(f"That's it, Christmas {self.target.year} is here Anywhere on Earth")
            self.target = self.target.replace(year=self.target.year + 1)

    async def broadcast(self, text: str):
        for ch in self.irc.channels:
            await self.irc.privmsg(ch, text)

    async def on_message(self, sender: str, reply_to: str, text: str):
        content = normalize(text)
        p = self.prefix
        try:
            if content in (f"{p}help", f"{p}xmas"):
                await self.irc.privmsg(reply_to, HELP_MSG.format(p=p))
            elif content == f"{p}source":
                await self.irc.privmsg(reply_to, f"Source code: {SOURCE_URL}")
            elif content == f"{p}remaining":
                remaining = len(self.zones) - self.index
                pct = (len(self.zones) - remaining) / len(self.zones) * 100
                s = "s" if remaining != 1 else ""
                await self.irc.privmsg(reply_to, f"{remaining} timezone{s} remaining. {pct:.2f}% are in the Christmas day")
            elif content.startswith(f"{p}next"):
                z = self.zones[self.index]
                now = datetime.now(timezone.utc)
                dur = timedelta(seconds=self.zone_offset_seconds(z))
                if now + dur >= self.target:
                    await self.irc.privmsg(reply_to, "No more next, Christmas is here AoE")
                else:
                    hdur = human_duration(self.target - dur - now)
                    await self.irc.privmsg(reply_to, f"Next Merry Christmas in {hdur} in {z.label()}")
            elif content.startswith(f"{p}prev") or content.startswith(f"{p}last"):
                prev_idx = self.index - 1 if self.index > 0 else len(self.zones) - 1
                z = self.zones[prev_idx]
                now = datetime.now(timezone.utc)
                dur = timedelta(seconds=self.zone_offset_seconds(z))
                target = self.target.replace(year=self.target.year - 1) if z.offset == -12 else self.target
                hdur = human_duration(now + dur - target)
                await self.irc.privmsg(reply_to, f"Previous Merry Christmas was {hdur} ago in {z.label()}")
            elif content == f"{p}time":
                await self.irc.privmsg(reply_to, "Time is " + datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 UTC %Y"))
            elif content.startswith(f"{p}time "):
                await self.cmd_time(reply_to, content[len(p) + 5:])
            elif content.startswith(f"{p}xmas "):
                await self.cmd_xmas(reply_to, content[len(p) + 5:])
        except Exception as exc:  # noqa: BLE001 - reply with the error, keep the bot alive
            await self.irc.privmsg(reply_to, f"error: {exc}")

    async def cmd_time(self, reply_to: str, arg: str):
        try:
            offset = resolve_offset(arg, self.abbrs)
            tz = timezone(timedelta(seconds=offset))
            now = datetime.now(tz)
            await self.irc.privmsg(reply_to, f"Time in {arg.upper()} is {now.strftime('%a %b %d %H:%M:%S %z')}")
            return
        except TZParseError:
            pass
        result = self.geocoder.tz_for(arg)
        if not result:
            await self.irc.privmsg(reply_to, "couldn't find that place")
            return
        tz, address = result
        now = datetime.now(tz)
        await self.irc.privmsg(reply_to, f"Time in {address} is {now.strftime('%a %b %d %H:%M:%S %z %Z')}")

    async def cmd_xmas(self, reply_to: str, arg: str):
        now = datetime.now(timezone.utc)
        try:
            offset = resolve_offset(arg, self.abbrs)
            eta = self.target - timedelta(seconds=offset) - now
            await self.irc.privmsg(reply_to, self._xmas_msg(arg.upper(), eta))
            return
        except TZParseError:
            pass
        result = self.geocoder.tz_for(arg)
        if not result:
            await self.irc.privmsg(reply_to, "couldn't find that place")
            return
        tz, address = result
        offset = now.astimezone(tz).utcoffset()
        eta = self.target - offset - now
        await self.irc.privmsg(reply_to, self._xmas_msg(address, eta))

    @staticmethod
    def _xmas_msg(place: str, eta: timedelta) -> str:
        if eta.total_seconds() > 0:
            return f"Merry Christmas in {place} will happen in {human_duration(eta)}"
        return f"Merry Christmas in {place} happened {human_duration(eta)} ago"


async def run(args):
    zones = load_zones()
    abbrs = build_abbr_table()
    geocoder = Geocoder(args.email, args.nominatim)
    irc = IRC(args.host, args.port, args.nick, args.channels, ssl_on=not args.no_ssl,
              password=args.password, sasl_user=args.sasl_nick, sasl_pass=args.sasl_pass)
    bot = Bot(irc=irc, prefix=args.prefix, zones=zones, abbrs=abbrs, geocoder=geocoder)

    while True:
        try:
            irc_task = asyncio.create_task(irc.run(bot.on_message))
            announce_task = asyncio.create_task(bot.announce_loop())
            done, pending = await asyncio.wait({irc_task, announce_task}, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            for t in done:
                t.result()
        except (ConnectionError, OSError) as exc:
            print(f"connection error: {exc}; reconnecting in 30s", file=sys.stderr)
            await asyncio.sleep(30)


NETWORK_DEFAULTS = {
    "port": 6697,
    "email": None,
    "nominatim": "https://nominatim.openstreetmap.org",
    "prefix": "!",
    "password": None,
    "sasl_nick": None,
    "sasl_pass": None,
    "no_ssl": False,
}
NETWORK_REQUIRED = ("host", "nick", "channels", "email")


def network_args(config: dict) -> argparse.Namespace:
    """Build a per-network args namespace from a --config entry, applying the
    same defaults and required fields as the single-network CLI flags."""
    missing = [f for f in NETWORK_REQUIRED if not config.get(f)]
    if missing:
        raise ValueError(f"network config missing required field(s): {', '.join(missing)}")
    merged = {**NETWORK_DEFAULTS, **config}
    return argparse.Namespace(**merged)


def parse_args():
    p = argparse.ArgumentParser(prog="pyxmasbot", description="Merry Christmas IRC bot")
    p.add_argument("--config", help="JSON file with a list of network configs, to run several networks at once")
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=NETWORK_DEFAULTS["port"])
    p.add_argument("--nick")
    p.add_argument("--channels", nargs="+", help="e.g. --channels '#test' '#test2'")
    p.add_argument("--email", help="contact email sent to Nominatim")
    p.add_argument("--nominatim", default=NETWORK_DEFAULTS["nominatim"])
    p.add_argument("--prefix", default=NETWORK_DEFAULTS["prefix"])
    p.add_argument("--password", default=None)
    p.add_argument("--sasl-nick", default=None)
    p.add_argument("--sasl-pass", default=None)
    p.add_argument("--no-ssl", action="store_true")
    args = p.parse_args()
    if not args.config:
        missing = [f for f in NETWORK_REQUIRED if not getattr(args, f)]
        if missing:
            p.error(f"the following arguments are required: {', '.join('--' + f.replace('_', '-') for f in missing)}")
    return args


async def main():
    args = parse_args()
    if args.config:
        networks = load_jsonc(args.config)
        await asyncio.gather(*(run(network_args(n)) for n in networks))
    else:
        await run(args)


if __name__ == "__main__":
    asyncio.run(main())
