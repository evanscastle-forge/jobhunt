"""FETCH: pull postings from public job-board feeds, once per scan.

    sources.py  one adapter per ATS and aggregator, plus the Job record they all produce

Stays a single module on purpose: the test suite swaps `sources.FETCHER` for a fake, and splitting
it into a package would leave that assignment on the package while the code read its own global.
"""
