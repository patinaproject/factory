# Price cache reads and one-hour cache writes

Task text version 1.0.0.

`spend.py` prices one Claude usage record. It ignores `cache_read_input_tokens`
and prices every cache write at the five-minute rate, even when the record's
`cache_creation` breakdown reports one-hour writes.

Make `cost` price uncached input, cache reads, five-minute cache writes,
one-hour cache writes, and output, each at its own rate in `RATES`. Keep the
command-line behavior: `python3 spend.py` reads one record from standard input
and prints `{"cost_usd": <number>}`.

Done when `python3 -m unittest discover -s tests` passes. Do not edit
`tests/test_spend.py`.
