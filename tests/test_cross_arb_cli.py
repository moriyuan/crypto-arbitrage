import json
from decimal import Decimal

import pytest

from btc_cta.cross_arb.__main__ import clients_from_auth, load_policy


def test_policy_decimal_values_and_unknown_fields(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"max_notional": "500", "max_cycles": 2}))
    assert load_policy(p).max_notional == Decimal("500")
    p.write_text(json.dumps({"live": True}))
    with pytest.raises(ValueError):
        load_policy(p)


def test_auth_permissions_and_symlink_rejected(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("{}")
    p.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        clients_from_auth(p)
    p.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(p)
    with pytest.raises(ValueError, match="private"):
        clients_from_auth(link)


def test_auth_cannot_target_external_or_wrong_worker_port(tmp_path):
    p = tmp_path / "auth.json"
    payload = {"schema_version": 1}
    for venue in ("bybit", "bitget"):
        payload[venue] = {
            "base_url": "http://example.com",
            "username": "test",
            "password": "test-only",
        }
    p.write_text(json.dumps(payload))
    p.chmod(0o600)
    with pytest.raises(ValueError, match="dedicated"):
        clients_from_auth(p)
