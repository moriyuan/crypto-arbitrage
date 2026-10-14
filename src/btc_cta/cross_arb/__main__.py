"""Bounded CLI for the Freqtrade cross-exchange dry-run pilot."""

from __future__ import annotations

import argparse
import json
import math
import stat
import time
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from .engine import VENUES, Coordinator, Journal, Policy
from .freqtrade import FreqtradeClient
from .market import Book, MarketData, MarketDataError


def load_policy(path: Path | None) -> Policy:
    if path is None:
        return Policy()
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or set(payload) - {f.name for f in fields(Policy)}:
        raise ValueError("invalid policy fields")
    defaults = Policy()
    for name, value in payload.items():
        if isinstance(getattr(defaults, name), Decimal):
            payload[name] = Decimal(str(value))
        elif isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("invalid policy number")
    return Policy(**payload)


def clients_from_auth(path: Path) -> dict[str, FreqtradeClient]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
        raise ValueError("API auth must be a regular private file (0600)")
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 1 or set(payload) != {"schema_version", *VENUES}:
        raise ValueError("invalid API auth schema")
    clients = {}
    try:
        for venue in VENUES:
            item = payload[venue]
            if set(item) != {"base_url", "username", "password"}:
                raise ValueError("invalid API auth fields")
            if item["base_url"] != f"http://127.0.0.1:{18081 if venue == 'bybit' else 18082}":
                raise ValueError("API worker endpoints must use the dedicated loopback ports")
            clients[venue] = FreqtradeClient(**item, timeout_seconds=3)
    except Exception:
        for client in clients.values():
            client.close()
        raise
    return clients


def report(engine: Coordinator, phase: str) -> dict[str, Any]:
    cycle = engine.state["cycle"]
    return {
        "mode": "dry_run",
        "phase": phase,
        "cycle_id": cycle["id"] if cycle else None,
        "reason": cycle["reason"] if cycle else engine.state["halt_reason"],
        "completed": engine.state["completed"],
        "realized_pnl": engine.state["realized_pnl"],
        "last_result": engine.state["last_result"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run", "flatten"))
    parser.add_argument("--state-dir", type=Path, default=Path("state/freqtrade-arb"))
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--resume", action="store_true", help="Resume a stopped, flat journal.")
    args = parser.parse_args()
    if (
        not math.isfinite(args.seconds)
        or not 0 < args.seconds <= 86400
        or not math.isfinite(args.interval)
        or not 0.5 <= args.interval <= 60
    ):
        parser.error("seconds must be in (0,86400], interval in [0.5,60]")
    clients: dict[str, FreqtradeClient] = {}
    journal = None
    code = 0
    try:
        policy = load_policy(args.policy)
        clients = clients_from_auth(args.state_dir / "api-auth.json")
        with MarketData(proxy=args.proxy) as market:
            if args.command == "check":
                for venue, client in clients.items():
                    config = client.preflight(venue, expect_dry_run=True)
                    print(
                        json.dumps(
                            {
                                "venue": venue,
                                "dry_run": config["dry_run"],
                                "state": config["state"],
                                "open_trades": len(client.open_trades()),
                            }
                        )
                    )
                rules, checked_books = market.fetch_rules(), market.fetch_books()
                for venue in VENUES:
                    print(
                        json.dumps(
                            {
                                "venue": venue,
                                "qty_step": str(rules[venue].qty_step),
                                "book_timestamp": checked_books[venue].exchange_ts_ms,
                                "request_ms": checked_books[venue].request_ms,
                            }
                        )
                    )
                return
            journal = Journal(args.state_dir / "coordinator.sqlite")
            engine = Coordinator(clients, journal, policy)
            if args.resume:
                if args.command != "run":
                    raise ValueError("resume applies only to run")
                engine.resume()
            if engine.state["cycle"] is None:
                engine.preflight()
            rules = {} if engine.state["cycle"] else market.fetch_rules()
            if args.command == "flatten":
                engine.request_exit()
            books: dict[str, Book] | None
            deadline = time.monotonic() + args.seconds
            phase = "STARTING"
            last = None
            while time.monotonic() < deadline:
                try:
                    books = None
                    if not engine.stopping:
                        try:
                            books = market.fetch_books()
                            if not rules:
                                rules = market.fetch_rules()
                        except MarketDataError:
                            books = None
                    phase = engine.tick(books, rules, int(time.time() * 1000))
                    current = report(engine, phase)
                    if current != last:
                        print(json.dumps(current), flush=True)
                        last = current
                    if engine.state["cycle"] is None and (engine.stopping or phase == "HALTED"):
                        break
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    print(
                        json.dumps(
                            {"phase": "RECONCILE_REQUIRED", "error_type": type(exc).__name__}
                        ),
                        flush=True,
                    )
                    engine.request_exit("read_or_protocol_failure")
                    time.sleep(args.interval)
            engine.request_exit("bounded_run_ended")
            drain_until = time.monotonic() + 30
            while engine.state["cycle"] is not None and time.monotonic() < drain_until:
                try:
                    phase = engine.tick(None, rules, int(time.time() * 1000))
                except Exception:
                    phase = "RECONCILE_REQUIRED"
                time.sleep(args.interval)
            if engine.state["cycle"] is not None:
                code = 2
                phase = "RECONCILE_REQUIRED"
            else:
                phase = "FLAT" if not engine.state["halt_reason"] else "HALTED"
            print(json.dumps(report(engine, phase)), flush=True)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "error_type": type(exc).__name__,
                    "message": "Operation stopped; inspect configuration or reconcile workers.",
                }
            )
        )
        code = 2
    finally:
        if journal:
            journal.close()
        for client in clients.values():
            client.close()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
