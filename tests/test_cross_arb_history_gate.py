from __future__ import annotations

from decimal import Decimal

import pytest

from btc_cta.cross_arb.history_gate import (
    DAY_MS,
    HOUR_MS,
    HistoryGate,
    HourlyBar,
    RollingHistoryConfig,
)

D = Decimal
START = 100 * DAY_MS
END = START + 20 * DAY_MS


def bars(rate: str = "0.001", *, days: int = 21, turnover: str = "100") -> list[HourlyBar]:
    price = D(100)
    result = []
    for hour in range(-1, days * 24):
        price *= D(1) + D(rate)
        result.append(HourlyBar(START + hour * HOUR_MS, price, D(turnover)))
    return result


def gate(data: dict | None = None, **kwargs: object) -> HistoryGate:
    source = data or {"AAAUSDT": {"bybit": bars(), "bitget": bars()}}
    return HistoryGate(
        RollingHistoryConfig(**kwargs), source, identity_verified_symbols={"AAAUSDT", "BBBUSDT"}
    )


def test_complete_window_and_delay() -> None:
    g = gate()
    assert not g.evaluate("AAAUSDT", END + 59999)["history_filter_passed"]
    row = g.evaluate("AAAUSDT", END + 60000)
    assert row["history_filter_passed"]
    assert row["history_window_start_ms"] == START
    assert row["history_window_end_ms"] == END
    assert D(row["bybit_adv20_usdt"]) == 2400
    assert 48 < D(row["bybit_rv20_bps"]) < 50


def test_twenty_days_mean_not_twenty_day_return() -> None:
    row = gate().evaluate("AAAUSDT", END + 60000)
    expected = D(10000) * ((D("1.001").ln() ** 2) * 24).sqrt()
    assert abs(D(row["bybit_rv20_bps"]) - expected) < D("0.00000001")


def test_threshold_is_inclusive_on_both_venues() -> None:
    source = {"AAAUSDT": {"bybit": bars(), "bitget": bars(turnover="99")}}
    assert gate(source, min_turnover_usdt=D(2376)).evaluate("AAAUSDT", END + 60000)[
        "history_filter_passed"
    ]
    assert (
        gate(source, min_turnover_usdt=D(2377)).evaluate("AAAUSDT", END + 60000)[
            "history_filter_reason"
        ]
        == "history_turnover_below_threshold"
    )
    first = gate().evaluate("AAAUSDT", END + 60000)
    assert gate(min_volatility_bps=D(first["bybit_rv20_bps"])).evaluate("AAAUSDT", END + 60000)[
        "history_filter_passed"
    ]


def test_rank_uses_lower_venue_and_identity_does_not_backfill() -> None:
    data = {
        "AAAUSDT": {"bybit": bars("0.1"), "bitget": bars("0.001")},
        "BBBUSDT": {"bybit": bars("0.002"), "bitget": bars("0.002")},
    }
    g = HistoryGate(RollingHistoryConfig(top_n=1), data, identity_verified_symbols={"AAAUSDT"})
    assert g.evaluate("AAAUSDT", END + 60000)["history_rank"] == 2
    assert g.evaluate("AAAUSDT", END + 60000)["history_filter_reason"] == "history_not_top_n"
    row = g.evaluate("BBBUSDT", END + 60000)
    assert row["history_rank"] == 1
    assert row["history_filter_reason"] == "asset_identity_unverified"


def test_tie_breaks_by_turnover_then_symbol() -> None:
    data = {
        s: {"bybit": bars(turnover=t), "bitget": bars(turnover=t)}
        for s, t in [("CCCUSDT", "100"), ("BBBUSDT", "200"), ("AAAUSDT", "200")]
    }
    g = gate(data, top_n=1)
    assert g.evaluate("AAAUSDT", END + 60000)["history_rank"] == 1
    assert g.evaluate("BBBUSDT", END + 60000)["history_rank"] == 2
    assert g.evaluate("CCCUSDT", END + 60000)["history_rank"] == 3


def test_missing_hour_fails_until_entire_window_recovers() -> None:
    broken = bars(days=22)
    broken.pop(1)
    g = gate({"AAAUSDT": {"bybit": broken, "bitget": bars(days=22)}})
    assert g.evaluate("AAAUSDT", END + 60000)["history_filter_reason"] == "history_incomplete"
    # Missing first hour is also required as the prior close for the first day,
    # so roll forward two days before the complete window recovers.
    assert g.evaluate("AAAUSDT", END + 2 * DAY_MS + 60000)["history_filter_passed"]


def test_current_day_and_future_changes_do_not_change_past_decision() -> None:
    original = bars(days=22)
    future = [
        HourlyBar(b.start_ms, b.close * (D(100) if b.start_ms >= END else 1), b.turnover_usdt)
        for b in original
    ]
    a = gate({"AAAUSDT": {"bybit": original, "bitget": original}})
    b = gate({"AAAUSDT": {"bybit": future, "bitget": future}})
    assert a.evaluate("AAAUSDT", END + 60000) == b.evaluate("AAAUSDT", END + 60000)
    assert a.evaluate("AAAUSDT", END + DAY_MS + 60000) != b.evaluate(
        "AAAUSDT", END + DAY_MS + 60000
    )


def test_daily_roll_changes_rank() -> None:
    a = bars(days=21)
    a = [
        HourlyBar(b.start_ms, b.close * (100 if b.start_ms >= END else 1), b.turnover_usdt)
        for b in a
    ]
    source = {
        "AAAUSDT": {"bybit": a, "bitget": a},
        "BBBUSDT": {"bybit": bars("0.002"), "bitget": bars("0.002")},
    }
    g = gate(source, top_n=1)
    assert not g.evaluate("AAAUSDT", END + 60000)["history_filter_passed"]
    assert g.evaluate("AAAUSDT", END + DAY_MS + 60000)["history_filter_passed"]


def test_inputs_and_outputs_cannot_mutate_frozen_history() -> None:
    source = {"AAAUSDT": {"bybit": bars(), "bitget": bars()}}
    g = gate(source)
    expected = g.evaluate("AAAUSDT", END + 60000)
    source["AAAUSDT"]["bybit"].clear()
    modified = g.evaluate("AAAUSDT", END + 60000)
    modified["history_rank"] = 999
    assert g.evaluate("AAAUSDT", END + 60000) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("window_days", 0),
        ("top_n", 0),
        ("publication_delay_ms", -1),
        ("min_turnover_usdt", D("-1")),
        ("min_volatility_bps", D("NaN")),
    ],
)
def test_invalid_config(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        RollingHistoryConfig(**{field: value})


@pytest.mark.parametrize(
    "close,turnover", [(D(0), D(1)), (D("NaN"), D(1)), (D(1), D(-1)), (D(1), D("Infinity"))]
)
def test_bad_hour_values_rejected(close: Decimal, turnover: Decimal) -> None:
    with pytest.raises(ValueError):
        HourlyBar(START, close, turnover)


def test_missing_other_venue_and_unknown_symbols_fail_closed() -> None:
    g = gate({"AAAUSDT": {"bybit": bars()}})
    assert not g.evaluate("AAAUSDT", END + 60000)["history_filter_passed"]
    assert (
        g.evaluate("NOPEUSDT", END + 60000)["history_filter_reason"] == "history_symbol_unavailable"
    )
    assert g.evaluate("BTCUSDT", END + 60000)["history_filter_reason"] == "asset_excluded"


def test_identity_is_required_and_duplicate_hours_rejected() -> None:
    data = {"AAAUSDT": {"bybit": bars(), "bitget": bars()}}
    g = HistoryGate(RollingHistoryConfig(), data)
    assert (
        g.evaluate("AAAUSDT", END + 60000)["history_filter_reason"] == "asset_identity_unverified"
    )
    data["AAAUSDT"]["bybit"].append(data["AAAUSDT"]["bybit"][0])
    with pytest.raises(ValueError, match="duplicate"):
        gate(data)


def _archive_fixture(path):
    import gzip
    import hashlib
    import json

    (path / "raw").mkdir()
    symbol = "AAAUSDT"
    bybit = {
        "symbol": symbol,
        "baseCoin": "AAA",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "symbolType": "",
        "tags": [],
    }
    bitget = {
        "symbol": symbol,
        "baseCoin": "AAA",
        "quoteCoin": "USDT",
        "symbolType": "perpetual",
        "symbolStatus": "normal",
        "isRwa": "NO",
        "supportMarginCoins": ["USDT"],
    }
    (path / "universe.json").write_text(
        json.dumps({"eligible": [{"symbol": symbol, "bybit": bybit, "bitget": bitget}]})
    )
    identity = {
        "status": "VERIFIED_CRYPTO_ONLY_TOP10",
        "source_universe_sha256": hashlib.sha256((path / "universe.json").read_bytes()).hexdigest(),
        "top10": [
            {
                "symbol": symbol,
                "asset_identity": {
                    "classification": "crypto_project_token",
                    "perpetual_symbol": symbol,
                    "official_project_identity_source": "https://example.test/project",
                },
            }
        ],
    }
    (path / "selection-crypto-verified.json").write_text(json.dumps(identity))
    manifest = {}
    for venue in ("bybit", "bitget"):
        rows = [
            [
                str(b.start_ms),
                str(b.close),
                str(b.close),
                str(b.close),
                str(b.close),
                str(b.turnover_usdt / b.close),
                str(b.turnover_usdt),
            ]
            for b in bars()
        ]
        params = {"symbol": symbol}
        if venue == "bybit":
            params.update(category="linear", interval="60")
            endpoint = "/v5/market/kline"
            response = {
                "retCode": 0,
                "result": {"symbol": symbol, "category": "linear", "list": rows},
            }
        else:
            params.update(productType="USDT-FUTURES", granularity="1H", kLineType="market")
            endpoint = "/api/v2/mix/market/candles"
            response = {"code": "00000", "data": rows}
        payload = {"venue": venue, "endpoint": endpoint, "params": params, "response": response}
        name = f"{venue}_{symbol}.json.gz"
        raw = gzip.compress(json.dumps(payload).encode())
        (path / "raw" / name).write_bytes(raw)
        manifest[name] = hashlib.sha256(raw).hexdigest()
    (path / "raw-manifest.json").write_text(json.dumps(manifest))


def _mutate_raw(path, mutate):
    import gzip
    import hashlib
    import json

    name = "bybit_AAAUSDT.json.gz"
    p = path / "raw" / name
    payload = json.loads(gzip.decompress(p.read_bytes()))
    mutate(payload)
    raw = gzip.compress(json.dumps(payload).encode())
    p.write_bytes(raw)
    manifest = json.loads((path / "raw-manifest.json").read_text())
    manifest[name] = hashlib.sha256(raw).hexdigest()
    (path / "raw-manifest.json").write_text(json.dumps(manifest))


def test_loader_uses_raw_history_and_not_saved_ranking(tmp_path) -> None:
    import json

    from btc_cta.cross_arb.history_gate import load_public_history

    _archive_fixture(tmp_path)
    g = load_public_history(tmp_path, RollingHistoryConfig())
    expected = g.evaluate("AAAUSDT", END + 60000)
    assert expected["history_filter_passed"]
    (tmp_path / "selection.json").write_text("not even valid JSON")
    p = tmp_path / "selection-crypto-verified.json"
    identity = json.loads(p.read_text())
    identity["top10"][0].update(verified_rank=999, rv20_min_pct=-999)
    p.write_text(json.dumps(identity))
    assert (
        load_public_history(tmp_path, RollingHistoryConfig()).evaluate("AAAUSDT", END + 60000)
        == expected
    )


def test_loader_rejects_hash_mismatch(tmp_path) -> None:
    from btc_cta.cross_arb.history_gate import load_public_history

    _archive_fixture(tmp_path)
    (tmp_path / "raw" / "bybit_AAAUSDT.json.gz").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_public_history(tmp_path, RollingHistoryConfig())


@pytest.mark.parametrize("kind", ["symbol", "turnover", "duplicate", "time", "interval"])
def test_loader_rejects_mismatched_or_invalid_bars(tmp_path, kind: str) -> None:
    from btc_cta.cross_arb.history_gate import load_public_history

    _archive_fixture(tmp_path)

    def mutate(payload):
        rows = payload["response"]["result"]["list"]
        if kind == "symbol":
            payload["response"]["result"]["symbol"] = "WRONGUSDT"
        elif kind == "turnover":
            rows[0][6] = "99999999999"
        elif kind == "duplicate":
            rows.append(rows[0])
        elif kind == "time":
            rows[0][0] = str(int(rows[0][0]) + 1)
        else:
            payload["params"]["interval"] = "1"

    _mutate_raw(tmp_path, mutate)
    with pytest.raises(ValueError):
        load_public_history(tmp_path, RollingHistoryConfig())


@pytest.mark.parametrize("missing", ["archive", "manifest_entry"])
def test_missing_raw_history_rejects_whole_ranking_pool(tmp_path, missing: str) -> None:
    import json

    from btc_cta.cross_arb.history_gate import load_public_history

    _archive_fixture(tmp_path)
    name = "bitget_AAAUSDT.json.gz"
    if missing == "archive":
        (tmp_path / "raw" / name).unlink()
    else:
        path = tmp_path / "raw-manifest.json"
        manifest = json.loads(path.read_text())
        del manifest[name]
        path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="incomplete history archive pool"):
        load_public_history(tmp_path, RollingHistoryConfig())


def test_directory_rwa_excluded_and_identity_snapshot_must_match(tmp_path) -> None:
    import hashlib
    import json

    from btc_cta.cross_arb.history_gate import load_public_history

    _archive_fixture(tmp_path)
    p = tmp_path / "universe.json"
    source = json.loads(p.read_text())
    source["eligible"][0]["bitget"]["isRwa"] = "YES"
    p.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="bound"):
        load_public_history(tmp_path, RollingHistoryConfig())
    p = tmp_path / "selection-crypto-verified.json"
    source = json.loads(p.read_text())
    source["source_universe_sha256"] = hashlib.sha256(
        (tmp_path / "universe.json").read_bytes()
    ).hexdigest()
    p.write_text(json.dumps(source))
    g = load_public_history(tmp_path, RollingHistoryConfig())
    assert (
        g.evaluate("AAAUSDT", END + 60000)["history_filter_reason"] == "history_symbol_unavailable"
    )
