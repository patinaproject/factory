# Keep unknown cost unknown

Task text version 1.0.0.

`spend.py` prices one Claude usage record. When the record names a model that
has no rate in `RATES`, it reports a cost of `0.0`. When the record has no usage,
it counts every category as zero and reports a cost of `0.0`. Both hide spend
that nobody priced.

Change `report` so that a record it cannot price reports
`{"cost_usd": null, "complete": false, "flags": [<reason>]}`, where the reason
names the missing rate or the missing usage. A record it can price reports
`{"cost_usd": <number>, "complete": true, "flags": []}`. Keep the command-line
behavior: `python3 spend.py` reads one record from standard input and prints
the report.

Done when `python3 -m unittest discover -s tests` passes. Do not edit
`tests/test_spend.py`.
