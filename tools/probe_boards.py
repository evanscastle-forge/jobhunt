#!/usr/bin/env python3
"""Retired. Board probing lives in the discovery pipeline now.

This verified a hand-written list of company names in tools/board_candidates.toml against the
Greenhouse, Lever and Ashby feeds. It could not propose a company you had not already typed, and it
carried its own HTTP stack -- a second user-agent, no retries, and no delay at all between 1,377
requests across three hosts.

`jobhunt.py discover` does the same probing and the part that was missing:

    uv run python jobhunt.py discover --harvest   collect employer names (no network)
    uv run python jobhunt.py discover             probe them, promote what carries work you want
    uv run python jobhunt.py discover --workday   sweep Workday for the ones no feed answered for
    uv run python jobhunt.py discover --status    what is known, and what is due for a probe

It still reads tools/board_candidates.toml, so nothing curated there is lost. Verified boards go to
companies.discovered.toml rather than being appended to config.toml, and data/companies.json
remembers every token tried so a re-run does not re-guess the dead ones.
"""
import sys

print(__doc__.strip(), file=sys.stderr)
sys.exit(1)
