"""Verification of count semantics, identity stability and run safety.

The bundled fixture serves 10 rows of which 9 are unique (the last row
repeats the first).  Fixture-control endpoints rehearse duplicate pages,
cross-page re-reports, rate limiting, failures and late content.
"""
import json
import sqlite3

import pytest
from starlette.testclient import TestClient

import mockapp.main as mock_main
from creatorvid import kernel, pipeline
from creatorvid.config import SPEC
from creatorvid.framework import identity, normalize
from creatorvid.mockdata import build_rows
from creatorvid.pipeline import run
from creatorvid.store import Store

BASE_UNIQUE = 9  # unique rows served by the bundled fixture


@pytest.fixture()
def fx():
    client = TestClient(mock_main.app)
    client.post("/fixture/reset")
    yield client
    client.post("/fixture/reset")


@pytest.fixture()
def con():
    c = sqlite3.connect(":memory:")
    Store(c)
    yield c
    c.close()


def make_row(i, value, ts="2030-02-01T00:00:00Z"):
    return {"id": "INJ-%04d" % i, "name": "Injected %d" % i, "value": value,
            "status": "active", "ts": ts}


def stored_by_id(con, rid):
    return [r for r in Store(con).all_records() if r.get("id") == rid]


def annotated(value, **extra):
    rec = normalize(dict({"id": "W-1", "name": "Work", "status": "active",
                          "ts": "2030-01-01T00:00:00Z", "value": value}, **extra))
    rec.update(kernel.process(rec))
    return rec


# --------------------------------------------------------------------- #
# Kernel: explainable, class-distinguishing count processing.
# --------------------------------------------------------------------- #
def test_process_explains_origin_scale_and_reason():
    out = kernel.process({"value": "1e6"})
    assert out["value_raw"] == "1e6"           # original expression
    assert out["scale_factor"] == 1000         # source scale
    assert out["count_reason"]                 # judgment reason
    assert out["count_rule"] == kernel.RULE_VERSION


@pytest.mark.parametrize("raw,cls,canonical", [
    (None, "empty", None),
    ("", "empty", None),
    ("-", "empty", None),
    ("n/a", "empty", None),
    ("not-a-number", "invalid", None),
    (True, "invalid", None),
    (float("nan"), "invalid", None),
    (float("inf"), "invalid", None),
    (0, "zero", 0),
    ("0.0", "zero", 0),
    (-3, "negative", None),
    ("-2.5", "negative", None),
    ("1e6", "scientific", 1000000),
    ("2.5E3", "scientific", 2500),
    (1000000, "large", 1000000),
    ("1,000,000", "large", 1000000),
    (7, "normal", 7),
    (10 ** 16, "drift", None),
    ("9.9e99", "drift", None),
])
def test_process_distinguishes_value_classes(raw, cls, canonical):
    out = kernel.process({"value": raw})
    assert out["count_class"] == cls
    assert out["value_canonical"] == canonical
    assert out["count_reason"]
    if canonical is None:
        assert cls in kernel.QUARANTINED


def test_process_keeps_legacy_scale_fields():
    out = kernel.process({"value": 2500000})
    assert (out["value_norm"], out["scale_factor"], out["was_large"]) == (2500.0, 1000, 1)
    out = kernel.process({"value": "garbage"})
    assert (out["value_norm"], out["scale_factor"], out["was_large"]) == (0.0, 1, 0)


# --------------------------------------------------------------------- #
# Identity: stable across formats, sources, order and rule updates.
# --------------------------------------------------------------------- #
def test_identity_is_format_and_annotation_independent():
    assert identity(annotated("1e6")) == identity(annotated(1000000))
    assert identity(annotated("1e6")) != identity(annotated(1000001))
    changed_rule = dict(annotated("1e6"), count_rule="count-v99")
    assert identity(changed_rule) == identity(annotated("1e6"))
    # anomalies keep their raw expression: distinct anomalies stay distinct
    assert identity(annotated("junk")) != identity(annotated(None))


def test_format_change_is_not_misjudged_as_growth(fx, con):
    fx.post("/fixture/inject", json={"rows": [make_row(1, "1e6")]})
    r1 = run(con=con)
    assert r1["count"] == BASE_UNIQUE + 1
    first = stored_by_id(con, "INJ-0001")
    assert len(first) == 1
    assert first[0]["value"] == "1e6"
    assert first[0]["value_canonical"] == 1000000
    assert first[0]["count_class"] == "scientific"

    # The source switches the same work to integer display.
    fx.post("/fixture/reset")
    fx.post("/fixture/inject", json={"rows": [make_row(1, 1000000)]})
    r2 = run(con=con)
    assert r2["ingested"] == 0
    assert r2["count"] == BASE_UNIQUE + 1  # no phantom growth
    again = stored_by_id(con, "INJ-0001")
    assert len(again) == 1
    assert again[0]["value"] == "1e6"  # original expression kept as history
    assert again[0]["count_class"] == "scientific"


def test_multi_source_delivery_order_keeps_count_meaning(fx, con):
    fx.post("/fixture/inject", json={"rows": [make_row(1, "1e6"), make_row(1, 1000000)]})
    r1 = run(con=con)

    other = sqlite3.connect(":memory:")
    Store(other)
    try:
        fx.post("/fixture/reset")
        fx.post("/fixture/inject", json={"rows": [make_row(1, 1000000), make_row(1, "1e6")]})
        r2 = run(con=other)
    finally:
        fx.post("/fixture/reset")

    assert r1["count"] == r2["count"] == BASE_UNIQUE + 1
    canon1 = sorted(r["value_canonical"] for r in Store(con).all_records())
    canon2 = sorted(r["value_canonical"] for r in Store(other).all_records())
    assert canon1 == canon2
    other.close()


def test_rule_update_does_not_rewrite_history(fx, con, monkeypatch):
    run(con=con)
    before = {r["id"]: r for r in Store(con).all_records()}
    assert all(r["count_rule"] == kernel.RULE_VERSION for r in before.values())

    monkeypatch.setattr(kernel, "RULE_VERSION", "count-v3")
    r2 = run(con=con)
    assert r2["ingested"] == 0
    assert r2["count"] == BASE_UNIQUE  # annotations never affect identity
    after = {r["id"]: r for r in Store(con).all_records()}
    assert after == before  # stored history untouched, still explained by the old rule


# --------------------------------------------------------------------- #
# Fetch hazards: duplicate pages, cross-page re-reports, rate limiting,
# failure recovery.
# --------------------------------------------------------------------- #
def test_duplicate_page_does_not_double_count(fx, con):
    fx.post("/fixture/repeat_page", params={"page": 2})
    r1 = run(con=con)
    assert r1["count"] == 5  # page 2 replayed page 1; deduped, not doubled

    fx.post("/fixture/reset")  # glitch cleared
    r2 = run(con=con)
    assert r2["count"] == BASE_UNIQUE
    assert r2["ingested"] == BASE_UNIQUE - 5  # only the missed rows are new


def test_cross_page_re_report_is_deduped(fx, con):
    re_reported = dict(build_rows()[0])  # page-1 row re-appears on a later page
    fx.post("/fixture/inject", json={"rows": [re_reported]})
    r = run(con=con)
    assert r["count"] == BASE_UNIQUE
    assert r["duplicates"] == 2  # fixture self-duplicate + re-reported copy


def test_rate_limited_fetch_recovers(fx, con):
    fx.post("/fixture/flaky", params={"n": 2, "status": 429})
    r = run(con=con)
    assert r["count"] == BASE_UNIQUE
    assert r["http_attempts"] == 5  # 2 retries on page 1 + 3 pages


def test_failed_run_leaves_no_partial_state(fx, con):
    fx.post("/fixture/flaky", params={"n": 5, "status": 500})
    with pytest.raises(RuntimeError):
        run(con=con)
    assert Store(con).count() == 0  # nothing partially stored
    r = run(con=con)  # fixture recovered; retry succeeds
    assert r["count"] == BASE_UNIQUE


# --------------------------------------------------------------------- #
# Late content, re-runs and continuations.
# --------------------------------------------------------------------- #
def test_late_content_is_not_lost_and_watermark_stays_monotonic(fx, con):
    r1 = run(con=con)
    wm1 = r1["watermark"]

    late = make_row(1, 12, ts="2029-12-01T00:00:00Z")  # older than the watermark
    fx.post("/fixture/inject", json={"rows": [late]})
    r2 = run(mode="incremental", con=con)
    assert r2["ingested"] == 1
    assert r2["count"] == BASE_UNIQUE + 1
    assert r2["watermark"] == wm1  # never regresses

    r3 = run(con=con)  # a full re-run converges to the same state
    assert r3["ingested"] == 0
    assert r3["count"] == BASE_UNIQUE + 1


def test_full_rerun_and_incremental_continuation(fx, con):
    r1 = run(mode="full", con=con)
    assert (r1["ingested"], r1["count"]) == (BASE_UNIQUE, BASE_UNIQUE)
    r2 = run(mode="full", con=con)
    assert (r2["ingested"], r2["count"]) == (0, BASE_UNIQUE)

    fx.get("/refresh")  # source publishes one new row
    r3 = run(mode="incremental", con=con)
    assert (r3["ingested"], r3["count"]) == (1, BASE_UNIQUE + 1)
    r4 = run(mode="incremental", con=con)
    assert (r4["ingested"], r4["count"]) == (0, BASE_UNIQUE + 1)


# --------------------------------------------------------------------- #
# Anomalous counts are quarantined and never pollute later pages.
# --------------------------------------------------------------------- #
def test_anomalous_counts_are_quarantined_without_polluting_the_run(fx, con):
    rows = [
        make_row(1, "9.99e15"),        # abnormal drift
        make_row(2, -7),               # negative
        make_row(3, "not-a-number"),   # invalid
        make_row(4, None),             # empty
        make_row(5, 42),               # normal
        make_row(6, "2.5e3"),          # scientific
    ]
    fx.post("/fixture/inject", json={"rows": rows})
    r = run(con=con)
    assert r["count"] == BASE_UNIQUE + 6  # stored for audit, not dropped
    assert r["anomalies"] == 4
    assert r["watermark"] == "2030-02-01T00:00:00Z"  # later pages processed

    by_id = {rec["id"]: rec for rec in Store(con).all_records()}
    assert by_id["INJ-0001"]["count_class"] == "drift"
    assert by_id["INJ-0002"]["count_class"] == "negative"
    assert by_id["INJ-0003"]["count_class"] == "invalid"
    assert by_id["INJ-0004"]["count_class"] == "empty"
    for rid in ("INJ-0001", "INJ-0002", "INJ-0003", "INJ-0004"):
        assert by_id[rid]["value_canonical"] is None
    assert by_id["INJ-0005"]["value_canonical"] == 42
    assert by_id["INJ-0006"]["value_canonical"] == 2500

    # Aggregate meaning: only countable values contribute.
    base_sum = sum(row["value"] for row in build_rows()[:BASE_UNIQUE])
    total = sum(rec["value_canonical"] for rec in Store(con).all_records()
                if rec["value_canonical"] is not None)
    assert total == base_sum + 42 + 2500

    r2 = run(con=con)  # the anomalies do not pollute subsequent runs either
    assert r2["ingested"] == 0
    assert r2["count"] == BASE_UNIQUE + 6


# --------------------------------------------------------------------- #
# Existing run modes remain usable.
# --------------------------------------------------------------------- #
def test_cli_entrypoint_still_works(fx, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("creatorvid.store.DB_PATH", tmp_path / "cli.db")
    from creatorvid.cli import main
    main([])
    out1 = json.loads(capsys.readouterr().out)
    assert out1["engine"] == SPEC.engine
    assert out1["count"] == BASE_UNIQUE
    main(["--incremental"])
    out2 = json.loads(capsys.readouterr().out)
    assert out2["count"] == BASE_UNIQUE
    assert out2["ingested"] == 0


def test_rpa_engine_mode_still_works(fx, con, monkeypatch):
    monkeypatch.setattr(SPEC, "engine", "rpa")
    r1 = pipeline.run(con=con)
    assert r1["engine"] == "rpa"
    assert r1["logged_in"] is True
    assert r1["count"] == BASE_UNIQUE
    r2 = pipeline.run(con=con)
    assert r2["ingested"] == 0
    assert r2["count"] == BASE_UNIQUE
