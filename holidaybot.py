#!/usr/bin/env python3
"""Shared core for the holiday-announcement IRC bots in this repo (pyxmasbot,
pynewyearbot): announces a given holiday as it arrives in each UTC offset.

A from-scratch reimplementation of https://github.com/TehPeGaSuS/xmasbot
(the original Go bot, Christmas-only) keeping the same user-facing behaviour
(commands, announcements, IRC colors) with far less code: one IRC connection,
no YAML/multi-network config, no code-gen pipeline for the
timezone-abbreviation table (built at runtime from the stdlib's zoneinfo
database instead of a bundled CSV + generator), generalized to any
month/day + holiday name so it isn't Christmas-specific.

Commands (prefix configurable, default "!"; <cmd> is e.g. "xmas" or "newyear"):
    !next                  time until the next announcement
    !previous / !prev      time since the last one
    !remaining             how many timezones are left this cycle
    !<cmd> <place|TZ|UTC+N> holiday status for a location, tz abbreviation, or UTC offset
    !time <place|TZ|UTC+N>  current time for a location, tz abbreviation, or UTC offset
    !time                   current UTC time
    !help                   list commands
    !source                 link to this bot's source
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import available_timezones, ZoneInfo

try:
    import requests
except ImportError:
    requests = None

HERE = Path(__file__).resolve().parent
SOURCE_URL = "https://github.com/TehPeGaSuS/holidaybot"  # repo for both bots
ORIGINAL_URL = "https://github.com/TehPeGaSuS/xmasbot"  # the Go bot this ports

# mIRC color codes (https://modern.ircdocs.horse/formatting.html), matching
# what xmasbot (green, 03) and newyearsbot (blue, 02) use.
IRC_BOLD = "\x02"
IRC_BLUE = "02"
IRC_GREEN = "03"
IRC_RED = "04"
IRC_YELLOW = "08"  # closest mIRC code to "gold"
IRC_RESET = "\x0f"


def col(s: str, enabled: bool, code: str = IRC_GREEN) -> str:
    """Bold + color, like the original bots' `bot.col()` (mIRC formatting)."""
    return f"{IRC_BOLD}\x03{code}{s}{IRC_RESET}" if enabled else s


# ---------------------------------------------------------------------------
# Timezone data: reuse the original project's country/city list (tz.json),
# plus a runtime-built abbreviation table (replaces the CSV + codegen tools).
# ---------------------------------------------------------------------------

@dataclass
class TZEntry:
    offset: float  # hours from UTC
    countries: list[dict]

    def label(self, colors: bool = False) -> str:
        parts = []
        for c in self.countries:
            cities = c.get("cities") or []
            name = f"{IRC_BOLD}{c['name']}{IRC_RESET}" if colors else c["name"]
            parts.append(f"{name} ({', '.join(cities)})" if cities else name)
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
    zones.sort(key=lambda z: z.offset, reverse=True)  # earliest to reach the holiday first
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
    """LocationIQ's display_name (Nominatim-compatible) is a full admin hierarchy (city, county,
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


def next_target(now: datetime, month: int, day: int) -> datetime:
    """This year's <month>/<day> 00:00 UTC while we're still on that UTC day
    or before it, otherwise next year's."""
    this_year = datetime(now.year, month, day, tzinfo=timezone.utc)
    year = now.year if now < this_year + timedelta(days=1) else now.year + 1
    return datetime(year, month, day, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# LocationIQ geocoding (Nominatim-compatible API, but with a usable free-tier
# rate limit instead of the public Nominatim instance's aggressive
# throttling), with a tiny in-memory cache and offline coordinate -> IANA tz
# lookup via timezonefinder.
# ---------------------------------------------------------------------------

class Geocoder:
    # LocationIQ's free tier allows 2 req/sec; stay comfortably under that.
    MIN_INTERVAL = 0.6

    def __init__(self, api_key: str, server: str):
        self.api_key = api_key
        self.server = server.rstrip("/")
        self.cache: dict[str, dict | None] = {}
        self.tz_cache: dict[tuple[str, str], str | None] = {}
        self._last_request = 0.0

    def _get(self, path: str, params: dict) -> dict | None:
        """GET a LocationIQ endpoint, rate-limited to MIN_INTERVAL between
        calls. Returns None on LocationIQ's "no match" 404, raises on other
        errors (429 gets a clear message instead of a raw HTTPError)."""
        if requests is None:
            raise RuntimeError("the 'requests' package is required for location lookups")
        wait = self.MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(
                f"{self.server}{path}",
                params={"key": self.api_key, **params},
                headers={"User-Agent": f"holidaybot: {SOURCE_URL}"},
                timeout=10,
            )
        finally:
            self._last_request = time.monotonic()
        if resp.status_code == 429:
            raise RuntimeError("LocationIQ is rate-limiting us right now, try again in a bit")
        if resp.status_code == 404:  # LocationIQ's "no match" response: {"error": "..."}
            return None
        resp.raise_for_status()
        return resp.json()

    def lookup(self, place: str) -> dict | None:
        if place in self.cache:
            return self.cache[place]
        data = self._get("/search.php", {"q": place, "format": "json", "accept-language": "en", "limit": 1})
        result = data[0] if data else None
        self.cache[place] = result
        return result

    def timezone_for(self, lat: str, lon: str) -> str | None:
        """IANA tz id for a coordinate, via LocationIQ's /timezone endpoint."""
        key = (lat, lon)
        if key in self.tz_cache:
            return self.tz_cache[key]
        data = self._get("/timezone", {"lat": lat, "lon": lon})
        tzid = data["timezone"]["name"] if data else None
        self.tz_cache[key] = tzid
        return tzid

    def tz_for(self, place: str) -> tuple[ZoneInfo, str] | None:
        result = self.lookup(place)
        if not result:
            return None
        tzid = self.timezone_for(result["lat"], result["lon"])
        if not tzid:
            return None
        return ZoneInfo(tzid), short_address(result["display_name"])


# ---------------------------------------------------------------------------
# Minimal asyncio IRC client (single server/network — the original supported
# a YAML list of networks; if you need more than one, run another process).
# ---------------------------------------------------------------------------

class IRC:
    def __init__(self, host, port, nick, channels, ssl_on=True, password=None,
                 sasl_user=None, sasl_pass=None, bind=None):
        self.host, self.port, self.nick = host, port, nick
        self.channels = channels
        self.ssl_on = ssl_on
        self.password = password
        self.sasl_user, self.sasl_pass = sasl_user, sasl_pass
        self.bind = bind  # local IPv4/IPv6 address to bind the outgoing connection to
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._last_send = 0.0
        self.joined = asyncio.Event()  # set once we've sent JOIN for all channels

    async def connect(self):
        ctx = ssl.create_default_context() if self.ssl_on else None
        local_addr = (self.bind, 0) if self.bind else None
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, ssl=ctx, local_addr=local_addr)
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

HELP_MSG = "Commands: '{p}{cmd} <location>', '{p}time <location>', '{p}next', '{p}previous', '{p}remaining', '{p}help', '{p}source'"


@dataclass
class Bot:
    irc: IRC
    prefix: str
    zones: list[TZEntry]
    abbrs: dict[str, int]
    geocoder: Geocoder
    holiday: str  # e.g. "Merry Christmas", "Happy New Year"
    cmd: str  # e.g. "xmas", "newyear" -- the "!<cmd> <location>" command name
    month: int
    day: int
    colors: bool = False
    primary_color: str = IRC_GREEN  # the holiday label / greeting
    secondary_color: str = IRC_GREEN  # the countdown duration / accents
    target: datetime = None
    index: int = 0

    def __post_init__(self):
        if self.target is None:
            self.target = next_target(datetime.now(timezone.utc), self.month, self.day)

    def zone_offset_seconds(self, z: TZEntry) -> int:
        return int(z.offset * 3600)

    def col_a(self, s: str) -> str:
        return col(s, self.colors, self.primary_color)

    def col_b(self, s: str) -> str:
        return col(s, self.colors, self.secondary_color)

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
                hdur = self.col_b(human_duration(timedelta(seconds=wait)))
                label = self.col_a("First " + self.holiday) if i == 0 else (
                    self.col_a("Final " + self.holiday) if i == len(self.zones) - 1 else self.col_a("Next " + self.holiday))
                await self.broadcast(f"{label} in {hdur} in {z.label(self.colors)}")
                await asyncio.sleep(max(wait, 0))
                await self.broadcast(f"{self.col_a(self.holiday)} in {z.label(self.colors)}")
            thats_it = self.col_a("That's it")
            year = self.col_b(str(self.target.year))
            aoe = self.col_b("Anywhere on Earth")
            await self.broadcast(f"{thats_it}, {self.col_a(self.holiday)} {year} is here {aoe}")
            self.target = self.target.replace(year=self.target.year + 1)

    async def broadcast(self, text: str):
        for ch in self.irc.channels:
            await self.irc.privmsg(ch, text)

    async def on_message(self, sender: str, reply_to: str, text: str):
        content = normalize(text)
        p = self.prefix
        try:
            if content in (f"{p}help", f"{p}{self.cmd}"):
                await self.irc.privmsg(reply_to, HELP_MSG.format(p=p, cmd=self.cmd))
            elif content == f"{p}source":
                await self.irc.privmsg(reply_to, f"Source code: {SOURCE_URL}")
            elif content == f"{p}remaining":
                remaining = len(self.zones) - self.index
                pct = (len(self.zones) - remaining) / len(self.zones) * 100
                s = "s" if remaining != 1 else ""
                await self.irc.privmsg(reply_to, f"{remaining} timezone{s} remaining. {pct:.2f}% are in the {self.col_a(self.holiday)} day")
            elif content.startswith(f"{p}next"):
                z = self.zones[self.index]
                now = datetime.now(timezone.utc)
                dur = timedelta(seconds=self.zone_offset_seconds(z))
                if now + dur >= self.target:
                    await self.irc.privmsg(reply_to, f"No more next, {self.col_a(self.holiday)} is here AoE")
                else:
                    hdur = self.col_b(human_duration(self.target - dur - now))
                    await self.irc.privmsg(reply_to, f"{self.col_a('Next ' + self.holiday)} in {hdur} in {z.label(self.colors)}")
            elif content.startswith(f"{p}prev") or content.startswith(f"{p}last"):
                prev_idx = self.index - 1 if self.index > 0 else len(self.zones) - 1
                z = self.zones[prev_idx]
                now = datetime.now(timezone.utc)
                dur = timedelta(seconds=self.zone_offset_seconds(z))
                target = self.target.replace(year=self.target.year - 1) if z.offset == -12 else self.target
                hdur = self.col_b(human_duration(now + dur - target))
                await self.irc.privmsg(reply_to, f"{self.col_a('Previous ' + self.holiday)} was {hdur} ago in {z.label(self.colors)}")
            elif content == f"{p}time":
                await self.irc.privmsg(reply_to, "Time is " + datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 UTC %Y"))
            elif content.startswith(f"{p}time "):
                await self.cmd_time(reply_to, content[len(p) + 5:])
            elif content.startswith(f"{p}{self.cmd} "):
                await self.cmd_holiday(reply_to, content[len(p) + len(self.cmd) + 1:])
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

    async def cmd_holiday(self, reply_to: str, arg: str):
        now = datetime.now(timezone.utc)
        try:
            offset = resolve_offset(arg, self.abbrs)
            eta = self.target - timedelta(seconds=offset) - now
            await self.irc.privmsg(reply_to, self._holiday_msg(arg.upper(), eta))
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
        await self.irc.privmsg(reply_to, self._holiday_msg(address, eta))

    def _holiday_msg(self, place: str, eta: timedelta) -> str:
        holiday = self.col_a(self.holiday)
        hdur = self.col_b(human_duration(eta))
        if eta.total_seconds() > 0:
            return f"{holiday} in {place} will happen in {hdur}"
        return f"{holiday} in {place} happened {hdur} ago"


@dataclass
class HolidaySpec:
    """What makes pyxmasbot.py and pynewyearbot.py different: everything else
    (IRC, geocoding, scheduling) is shared."""
    name: str  # e.g. "Merry Christmas"
    cmd: str  # e.g. "xmas" -> "!xmas <location>"
    month: int
    day: int
    prog: str  # argv[0] name, for --help
    primary_color: str = IRC_GREEN
    secondary_color: str = IRC_GREEN


async def run(args, spec: HolidaySpec):
    zones = load_zones()
    abbrs = build_abbr_table()
    geocoder = Geocoder(args.api_key, args.geocoder_url)
    irc = IRC(args.host, args.port, args.nick, args.channels, ssl_on=not args.no_ssl,
              password=args.password, sasl_user=args.sasl_nick, sasl_pass=args.sasl_pass,
              bind=args.bind)
    bot = Bot(irc=irc, prefix=args.prefix, zones=zones, abbrs=abbrs, geocoder=geocoder,
              holiday=spec.name, cmd=spec.cmd, month=spec.month, day=spec.day, colors=args.colors,
              primary_color=spec.primary_color, secondary_color=spec.secondary_color)

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


API_KEY_ENV_VAR = "LOCATIONIQ_API_KEY"


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (without
    overriding variables already set in the real environment). No new
    dependency for this -- same spirit as strip_json_comments()."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def network_defaults() -> dict:
    """Re-read $LOCATIONIQ_API_KEY each call, so a .env file loaded after
    import (see load_dotenv) is picked up."""
    return {
        "port": 6697,
        "api_key": os.environ.get(API_KEY_ENV_VAR),
        "geocoder_url": "https://us1.locationiq.com/v1",
        "prefix": "!",
        "password": None,
        "sasl_nick": None,
        "sasl_pass": None,
        "no_ssl": False,
        "colors": False,
        "bind": None,
    }


# Always required per network/CLI invocation. api_key is checked separately
# since it can also come from $LOCATIONIQ_API_KEY (see network_defaults()).
NETWORK_REQUIRED = ("host", "nick", "channels")


def _check_required(values: dict) -> list[str]:
    missing = [f for f in NETWORK_REQUIRED if not values.get(f)]
    if not values.get("api_key"):
        missing.append("api_key")
    return missing


def network_args(config: dict, shared: dict | None = None) -> argparse.Namespace:
    """Build a per-network args namespace from a --config entry, applying
    (lowest to highest priority) the same defaults as the CLI flags, then
    the file's top-level shared settings (e.g. api_key), then the entry's
    own fields."""
    merged = {**network_defaults(), **(shared or {}), **config}
    missing = _check_required(merged)
    if missing:
        raise ValueError(f"network config missing required field(s): {', '.join(missing)}")
    return argparse.Namespace(**merged)


def load_config(path: str) -> tuple[dict, list[dict]]:
    """Load a --config file: either the plain `[{...}, {...}]` list of
    networks, or `{"api_key": "...", ..., "networks": [...]}` where any
    top-level field besides "networks" is shared by every entry unless that
    entry overrides it."""
    data = load_jsonc(path)
    if isinstance(data, list):
        return {}, data
    networks = data.pop("networks", [])
    return data, networks


def parse_args(spec: HolidaySpec):
    defaults = network_defaults()
    p = argparse.ArgumentParser(prog=spec.prog, description=f"{spec.name} IRC bot")
    p.add_argument("--config", help="JSON file with a list of network configs, to run several networks at once")
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=defaults["port"])
    p.add_argument("--nick")
    p.add_argument("--channels", nargs="+", help="e.g. --channels '#test' '#test2'")
    p.add_argument("--api-key", default=defaults["api_key"],
                    help=f"LocationIQ API key (default: ${API_KEY_ENV_VAR})")
    p.add_argument("--geocoder-url", default=defaults["geocoder_url"])
    p.add_argument("--prefix", default=defaults["prefix"])
    p.add_argument("--password", default=None)
    p.add_argument("--sasl-nick", default=None)
    p.add_argument("--sasl-pass", default=None)
    p.add_argument("--no-ssl", action="store_true")
    p.add_argument("--colors", action="store_true", help="use IRC bold/color formatting in messages")
    p.add_argument("--bind", default=None, help="local IPv4/IPv6 address to bind the outgoing connection to")
    args = p.parse_args()
    if not args.config:
        missing = _check_required(vars(args))
        if missing:
            p.error(f"the following arguments are required: {', '.join('--' + f.replace('_', '-') for f in missing)}"
                     f" (or set ${API_KEY_ENV_VAR} for api-key)")
    return args


async def main(spec: HolidaySpec):
    load_dotenv()
    args = parse_args(spec)
    if args.config:
        shared, networks = load_config(args.config)
        await asyncio.gather(*(run(network_args(n, shared), spec) for n in networks))
    else:
        await run(args, spec)
