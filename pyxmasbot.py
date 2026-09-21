#!/usr/bin/env python3
"""PyXmasBot: a Merry Christmas IRC bot. See holidaybot.py for how it works."""
import asyncio

import holidaybot

SPEC = holidaybot.HolidaySpec(
    name="Merry Christmas", cmd="xmas", month=12, day=25, prog="pyxmasbot",
    primary_color=holidaybot.IRC_RED, secondary_color=holidaybot.IRC_GREEN,
)

if __name__ == "__main__":
    asyncio.run(holidaybot.main(SPEC))
