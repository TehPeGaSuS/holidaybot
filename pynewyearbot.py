#!/usr/bin/env python3
"""PyNewYearBot: a Happy New Year IRC bot. See holidaybot.py for how it works."""
import asyncio

import holidaybot

SPEC = holidaybot.HolidaySpec(
    name="Happy New Year", cmd="newyear", month=1, day=1, prog="pynewyearbot",
    primary_color=holidaybot.IRC_YELLOW, secondary_color=holidaybot.IRC_BLUE,
)

if __name__ == "__main__":
    asyncio.run(holidaybot.main(SPEC))
