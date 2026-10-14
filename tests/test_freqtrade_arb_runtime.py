from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

RUNTIME_PATH = Path(__file__).parents[1] / "deploy" / "freqtrade-arb" / "runtime.py"
spec = importlib.util.spec_from_file_location("freqtrade_arb_runtime", RUNTIME_PATH)
assert spec is not None and spec.loader is not None
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_prepare_private_independent_dry_run_instances(tmp_path: Path) -> None:
    auth_path = runtime.prepare(tmp_path)
    auth = json.loads(auth_path.read_text())
    assert auth["bybit"]["password"] != auth["bitget"]["password"]
    for venue, port in runtime.VENUES.items():
        config_path = runtime.validated_config(tmp_path, venue)
        config = json.loads(config_path.read_text())
        assert config["dry_run"] is True
        assert config["trading_mode"] == "futures"
        assert config["margin_mode"] == "isolated"
        assert config["max_open_trades"] == 1
        assert config["exchange"]["pair_whitelist"] == ["BTC/USDT:USDT"]
        assert all(config["exchange"][field] == "" for field in ("key", "secret", "password"))
        assert config["api_server"]["listen_ip_address"] == "127.0.0.1"
        assert config["api_server"]["listen_port"] == port
        assert config["db_url"].endswith(f"/{venue}.sqlite")
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    runtime.prepare(tmp_path)
    assert json.loads(auth_path.read_text()) == auth


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config.update(dry_run=False),
        lambda config: config.update(add_config_files=["live.json"]),
        lambda config: config["api_server"].update(listen_ip_address="0.0.0.0"),
        lambda config: config["api_server"].update(password="weak"),
        lambda config: config["exchange"].update(key="real-key"),
        lambda config: config.update(max_open_trades=2),
    ],
)
def test_run_rejects_unsafe_config_changes(tmp_path: Path, mutation) -> None:
    runtime.prepare(tmp_path)
    config_path = tmp_path / "bybit.json"
    config = json.loads(config_path.read_text())
    mutation(config)
    runtime.private_json(config_path, config)
    with pytest.raises(runtime.RuntimeValidationError):
        runtime.validated_config(tmp_path, "bybit")


def test_state_inside_repository_must_be_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    with pytest.raises(runtime.RuntimeValidationError, match="ignored state"):
        runtime.prepare(tmp_path / "deploy" / "private")
    runtime.prepare(tmp_path / "state" / "freqtrade-arb")


def test_state_rejects_symlink_and_public_auth(tmp_path: Path) -> None:
    auth_path = runtime.prepare(tmp_path)
    auth_path.chmod(0o644)
    with pytest.raises(runtime.RuntimeValidationError, match="0600"):
        runtime.validated_config(tmp_path, "bybit")
    auth_path.chmod(0o600)
    other = tmp_path / "auth-backup.json"
    auth_path.rename(other)
    auth_path.symlink_to(other)
    with pytest.raises(runtime.RuntimeValidationError, match="regular"):
        runtime.prepare(tmp_path)


def test_literal_loader_never_executes_shell(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    source = tmp_path / "zshrc"
    source.write_text(
        "export BYBIT_API_KEY='bybit-key' # comment\n"
        'BYBIT_SECRET_KEY="bybit-secret"\n'
        f"export BITGET_API_KEY=$(touch {marker})\n"
        "BITGET_SECRET_KEY=`arbitrary command`\n"
        "BITGET_PASSPHRASE=${UNKNOWN}\n"
        "UNRELATED_SECRET='unrelated-value'\n"
    )
    values, rejected = runtime.read_literal_credentials(source)
    assert values == {"BYBIT_API_KEY": "bybit-key", "BYBIT_SECRET_KEY": "bybit-secret"}
    assert rejected == {"BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE"}
    assert not marker.exists()
    assert runtime.load_exchange_credentials(source, "bybit") == {
        "key": "bybit-key",
        "secret": "bybit-secret",
    }
    with pytest.raises(runtime.RuntimeValidationError, match="bitget"):
        runtime.load_exchange_credentials(source, "bitget")
    report = json.dumps(runtime.credential_presence(source))
    assert "bybit-key" not in report and "bybit-secret" not in report
    assert "unrelated-value" not in report and "UNRELATED_SECRET" not in report


def test_literal_loader_accepts_passphrase_and_rejects_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "zshrc"
    source.write_text(
        "export BITGET_API_KEY=plain-key\n"
        "export BITGET_SECRET_KEY='literal-secret'\n"
        "export BITGET_PASSPHRASE='pass phrase!$literal'\n"
        "BYBIT_API_KEY='first'\n"
        "BYBIT_API_KEY='second'\n"
    )
    assert runtime.load_exchange_credentials(source, "bitget")["password"] == "pass phrase!$literal"
    values, rejected = runtime.read_literal_credentials(source)
    assert "BYBIT_API_KEY" not in values
    assert rejected == {"BYBIT_API_KEY"}


def test_run_scrubs_ambient_config_and_exchange_secrets(tmp_path: Path, monkeypatch) -> None:
    runtime.prepare(tmp_path)
    monkeypatch.setenv("FREQTRADE__DRY_RUN", "false")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "hidden-key")
    monkeypatch.setenv("BYBIT_API_KEY", "hidden-key")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="Freqtrade Version:\tfreqtrade 2026.9\n",
        ),
    )
    captured = {}
    monkeypatch.setattr(
        runtime.os,
        "execvpe",
        lambda executable, arguments, environment: captured.update(
            executable=executable, args=arguments, env=environment
        ),
    )
    runtime.run(tmp_path, "bybit", "/pinned/freqtrade")
    assert captured["executable"] == "/pinned/freqtrade"
    assert "--config" in captured["args"]
    assert not any(key.startswith("FREQTRADE__") for key in captured["env"])
    assert "BYBIT_API_KEY" not in captured["env"]


def test_run_rejects_wrong_version_before_start(tmp_path: Path, monkeypatch) -> None:
    runtime.prepare(tmp_path)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="Freqtrade Version:\tfreqtrade 2026.10\n",
        ),
    )
    monkeypatch.setattr(runtime.os, "execvpe", lambda *a: pytest.fail("Must not start"))
    with pytest.raises(runtime.RuntimeValidationError, match="pinned"):
        runtime.run(tmp_path, "bybit", "freqtrade")


@pytest.mark.parametrize("remove_telegram_fields", [False, True])
def test_pinned_freqtrade_schema_and_consistency(
    tmp_path: Path, remove_telegram_fields: bool
) -> None:
    """Opt-in integration: FREQTRADE_TEST_PYTHON points at the pinned environment."""
    executable = os.environ.get("FREQTRADE_TEST_PYTHON")
    if not executable:
        pytest.skip("Set FREQTRADE_TEST_PYTHON to the pinned Freqtrade Python interpreter.")
    runtime.prepare(tmp_path)
    script = """
import json
import logging
import sys
from pathlib import Path
import freqtrade
from freqtrade.configuration.config_validation import validate_config_consistency
from freqtrade.enums import RunMode
logging.disable(logging.CRITICAL)
if freqtrade.__version__ != '2026.9':
    sys.exit(3)
reject_expected = sys.argv[2] == 'True'
for venue in ('bybit', 'bitget'):
    config = json.loads((Path(sys.argv[1]) / (venue + '.json')).read_text())
    config['runmode'] = RunMode.DRY_RUN
    if reject_expected:
        config['telegram'] = {'enabled': False}
    try:
        validate_config_consistency(config)
    except Exception:
        if not reject_expected:
            sys.exit(2)
    else:
        if reject_expected:
            sys.exit(2)
"""
    result = subprocess.run(
        [executable, "-c", script, str(tmp_path), str(remove_telegram_fields)],
        capture_output=True,
        check=False,
        env=runtime.child_environment(),
        timeout=30,
    )
    if result.returncode:
        pytest.fail("Pinned Freqtrade schema/consistency validation failed; outputs redacted.")


@pytest.fixture
def strategy_class(monkeypatch):
    # Unit-test the adapter's own guards without requiring Freqtrade as a project dependency.
    exceptions = ModuleType("freqtrade.exceptions")
    exceptions.OperationalException = RuntimeError
    strategy = ModuleType("freqtrade.strategy")
    strategy.IStrategy = object
    monkeypatch.setitem(sys.modules, "freqtrade.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "freqtrade.strategy", strategy)
    source = RUNTIME_PATH.parent / "strategies" / "CrossExchangeArb.py"
    spec = importlib.util.spec_from_file_location("cross_exchange_arb_strategy", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CrossExchangeArb


def test_strategy_never_generates_entries_or_exits(strategy_class) -> None:
    strategy = strategy_class()
    frame = pd.DataFrame({"close": [100, 101], "enter_long": [1, 1], "exit_short": [1, 1]})
    strategy.populate_entry_trend(frame, {})
    strategy.populate_exit_trend(frame, {})
    assert not frame[["enter_long", "enter_short", "exit_long", "exit_short"]].any().any()
    assert strategy.minimal_roi == {}
    assert strategy.position_adjustment_enable is False
    assert strategy.order_types["force_exit"] == "market"


def test_strategy_refuses_live_and_noncoordinator_entries(strategy_class) -> None:
    strategy = strategy_class()
    strategy.config = {"dry_run": False, "exchange": {"name": "bybit"}}
    with pytest.raises(RuntimeError, match="dry-run only"):
        strategy.bot_start()
    kwargs = {
        "pair": "BTC/USDT:USDT",
        "order_type": "limit",
        "amount": 0.001,
        "rate": 100000.0,
        "time_in_force": "GTC",
        "current_time": datetime.now(UTC),
        "entry_tag": "ca1-example:bybit",
        "side": "long",
    }
    assert strategy.confirm_trade_entry(**kwargs) is False
    strategy.config["dry_run"] = True
    strategy.bot_start()
    assert strategy.confirm_trade_entry(**kwargs) is True
    for mutation in (
        {"entry_tag": "force_entry"},
        {"entry_tag": "ca1-example:bitget"},
        {"pair": "ETH/USDT:USDT"},
        {"order_type": "market"},
        {"amount": float("nan")},
    ):
        assert strategy.confirm_trade_entry(**{**kwargs, **mutation}) is False
