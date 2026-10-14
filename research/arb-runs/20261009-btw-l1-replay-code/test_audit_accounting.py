"""Small contract checks; no production ledger scan or network access."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import audit_accounting as audit


class AccountingContractTests(unittest.TestCase):
    def metadata(self):
        return {
            "symbol": "BTWUSDT", "requested_day_utc": "2026-10-01",
            "execution_enabled": False, "prediction_model_used": False,
            "initial_balance_per_venue": "1000", "execution_book_levels": 1,
            "depth_sizing": {"band_bps": "0", "participation": "0.25"},
            "policy": dict(audit.EXPECTED_POLICY),
        }

    def test_expected_l1_contract(self):
        self.assertEqual(audit.l1_policy_errors(self.metadata()), [])

    def test_old_multilevel_or_guard_rejected(self):
        for field, value, label in (
            ("execution_book_levels", 50, "execution_book_levels"),
            ("initial_balance_per_venue", "2000", "initial_balance_per_venue"),
        ):
            meta = self.metadata()
            meta[field] = value
            self.assertIn(label, audit.l1_policy_errors(meta))
        meta = self.metadata()
        meta["policy"]["price_guard_bps"] = "1"
        self.assertIn("policy:price_guard_bps", audit.l1_policy_errors(meta))

    def test_missing_metadata_rejected(self):
        self.assertIn("execution_book_levels", audit.l1_policy_errors({}))

    def test_top1_metadata_alias(self):
        meta = self.metadata()
        del meta["execution_book_levels"]
        meta["depth_sizing"]["max_levels"] = 1
        self.assertEqual(audit.l1_policy_errors(meta), [])

    def test_price_must_equal_limit_for_entry_and_exit(self):
        rules = {"bybit": {"price_tick": "0.00001"}}
        for intent in ("entry", "exit"):
            row = {"venue": "bybit", "price": "1.25001", "limit_price": "1.25001", "intent": intent}
            self.assertEqual(audit.l1_order_errors(row, rules), [])
            wrong = dict(row, limit_price="1.25002")
            self.assertIn("filled_price_not_limit", audit.l1_order_errors(wrong, rules))
            blank = dict(row, limit_price="")
            self.assertIn("missing_or_invalid_price_limit_tick", audit.l1_order_errors(blank, rules))

    def test_vwap_off_tick_rejected(self):
        rules = {"bybit": {"price_tick": "0.00001"}}
        row = {"venue": "bybit", "price": "1.250015", "limit_price": "1.250015"}
        self.assertIn("price_or_limit_off_tick_grid", audit.l1_order_errors(row, rules))

    def test_running_guard_does_not_open_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "status.json").write_text(json.dumps({"status": "RUNNING"}))
            self.assertEqual(audit.main(root), 3)
            self.assertFalse((root / "ledger.sqlite").exists())
            self.assertFalse((root / "acceptance.json").exists())

    def test_existing_acceptance_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "status.json").write_text(json.dumps({"status": "COMPLETED"}))
            result = root / "acceptance.json"
            result.write_text("preserved")
            with self.assertRaisesRegex(RuntimeError, "refuse_to_overwrite"):
                audit.main(root)
            self.assertEqual(result.read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
