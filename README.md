# creatorvid

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/creatorvid`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Count semantics

`creatorvid.kernel` canonicalizes the `value` field so a pure display-format
change (for example `"1e6"` vs `1000000`) is never mistaken for real growth.
Each record is annotated with:

- `value_raw` — the original expression as received;
- `scale_factor` / `value_norm` / `was_large` — the source scale (legacy view);
- `value_canonical` — the format-independent count, or `null` when the value
  is not countable;
- `count_class` — one of `empty`, `invalid`, `zero`, `negative`,
  `scientific`, `large`, `normal`, `drift`;
- `count_reason` — why the decision was made;
- `count_rule` — the rule version that produced the annotation.

Record identity (`creatorvid.framework.identity`) is computed from source
fields only, with countable values in canonical form. Derived annotations
never take part in identity, so a rule update cannot duplicate or silently
rewrite stored history, and re-collection or multi-source delivery in any
order converges to the same count. Anomalous counts (empty, invalid,
negative, abnormal drift) are quarantined: stored for audit but excluded
from `value_canonical`. The watermark is a monotonic checkpoint; both full
and incremental runs are idempotent and safe to repeat.

The fixture service also exposes `/fixture/*` control endpoints (reset,
inject, flaky, repeat_page) used by the test suite to rehearse duplicate
pages, cross-page re-reports, rate limiting, failures and late content.
