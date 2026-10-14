#!/usr/bin/env python3
"""Prepare or start two pinned Freqtrade dry-run instances without exchange secrets.

The optional credentials-check command parses only the five authorized literal
assignments. It never sources shell startup files or sends private API requests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VERSION = "2026.9"
COMMIT = "1f394eaebc2f46a83d26971388628707802f8602"
VENUES = {"bybit": 18081, "bitget": 18082}
CREDENTIAL_NAMES = {
    "bybit": {"key": "BYBIT_API_KEY", "secret": "BYBIT_SECRET_KEY"},
    "bitget": {
        "key": "BITGET_API_KEY",
        "secret": "BITGET_SECRET_KEY",
        "password": "BITGET_PASSPHRASE",
    },
}
ALLOWED_NAMES = {name for fields in CREDENTIAL_NAMES.values() for name in fields.values()}
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# Reject expansion, command substitution, concatenation, shell operators and escapes.
LITERAL = re.compile(r"""^\s*('([^']*)'|"([^"$`\\]*)"|([A-Za-z0-9_./:@%+,=!-]+))\s*(?:\#.*)?$""")


class RuntimeValidationError(ValueError):
    """A redacted error safe to display on the command line."""


def read_literal_credentials(path: Path) -> tuple[dict[str, str], set[str]]:
    """Return approved literals in memory and names that need manual correction.

    This intentionally accepts a small subset of zsh syntax; a rejected assignment
    is never evaluated. Duplicates fail closed, including valid-then-invalid ones.
    No file contents or credential values enter exception messages.
    """
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeValidationError("Credential file exceeds the supported size.")
    values: dict[str, str] = {}
    rejected: set[str] = set()
    seen: set[str] = set()
    for line in path.read_text().splitlines():
        match = ASSIGNMENT.fullmatch(line)
        if match is None or match[1] not in ALLOWED_NAMES:
            continue
        name = match[1]
        literal = LITERAL.fullmatch(match[2])
        if name in seen or literal is None:
            rejected.add(name)
            values.pop(name, None)
        elif name not in rejected:
            values[name] = next(value for value in literal.groups()[1:] if value is not None)
        seen.add(name)
    return values, rejected


def load_exchange_credentials(path: Path, venue: str) -> dict[str, str]:
    """Explicit in-memory loader for future authorized integrations; never used by run.

    Do not print the return value or persist it. This loader does not authorize or
    enable live execution; the bundled strategy and launcher remain dry-run only.
    """
    if venue not in VENUES:
        raise RuntimeValidationError("Unsupported exchange.")
    values, rejected = read_literal_credentials(path)
    fields = CREDENTIAL_NAMES[venue]
    if any(name in rejected or not values.get(name) for name in fields.values()):
        raise RuntimeValidationError(f"Missing or nonliteral credentials for {venue}.")
    return {field: values[name] for field, name in fields.items()}


def credential_presence(path: Path) -> dict[str, dict[str, str]]:
    values, rejected = read_literal_credentials(path)
    return {
        venue: {
            name: "rejected_nonliteral_or_duplicate"
            if name in rejected
            else "present"
            if values.get(name)
            else "missing_or_empty"
            for name in fields.values()
        }
        for venue, fields in CREDENTIAL_NAMES.items()
    }


def state_directory(path: Path) -> Path:
    if path.is_symlink():
        raise RuntimeValidationError("State directory may not be a symlink.")
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(ROOT) and not resolved.is_relative_to(ROOT / "state"):
        raise RuntimeValidationError("Inside the repository, state must be under ignored state/.")
    return resolved


def private_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise RuntimeValidationError("Refusing to replace a symlink in state.")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeValidationError("Missing regular runtime state file; run prepare first.")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeValidationError("Runtime state files must have permissions 0600.")
    try:
        result = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise RuntimeValidationError("Invalid runtime JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeValidationError("Runtime JSON must be an object.")
    return result


def validate_auth(auth: dict[str, Any]) -> None:
    if auth.get("schema_version") != 1:
        raise RuntimeValidationError("Unsupported auth manifest schema.")
    for venue, port in VENUES.items():
        item = auth.get(venue, {})
        if (
            not isinstance(item, dict)
            or item.get("base_url") != f"http://127.0.0.1:{port}"
            or item.get("username") != "coordinator"
            or not isinstance(item.get("password"), str)
            or len(item["password"]) < 32
        ):
            raise RuntimeValidationError("Invalid loopback API authentication manifest.")


def prepare(path: Path) -> Path:
    state = state_directory(path)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    auth_path = state / "api-auth.json"
    if auth_path.exists() or auth_path.is_symlink():
        auth = read_private_json(auth_path)
        validate_auth(auth)
    else:
        auth = {"schema_version": 1}
        for venue, port in VENUES.items():
            auth[venue] = {
                "base_url": f"http://127.0.0.1:{port}",
                "username": "coordinator",
                "password": secrets.token_urlsafe(48),
            }
        private_json(auth_path, auth)
    for venue in VENUES:
        config = json.loads((HERE / f"{venue}.json").read_text())
        config["api_server"].update(
            {
                "enabled": True,
                "username": auth[venue]["username"],
                "password": auth[venue]["password"],
                "jwt_secret_key": secrets.token_urlsafe(48),
                "ws_token": secrets.token_urlsafe(48),
            }
        )
        config["db_url"] = f"sqlite:///{state / (venue + '.sqlite')}"
        user_data = state / (venue + "-user-data")
        user_data.mkdir(exist_ok=True, mode=0o700)
        config["user_data_dir"] = str(user_data)
        private_json(state / f"{venue}.json", config)
    return auth_path


def validated_config(state: Path, venue: str) -> Path:
    config_path = state / f"{venue}.json"
    config = read_private_json(config_path)
    auth = read_private_json(state / "api-auth.json")
    validate_auth(auth)
    # Only generated keys are accepted: prevents additional config files/strategies
    # from silently changing execution or injecting credentials.
    template = json.loads((HERE / f"{venue}.json").read_text())
    expected = dict(template)
    expected["api_server"] = dict(config.get("api_server", {}))
    expected["db_url"] = f"sqlite:///{state / (venue + '.sqlite')}"
    expected["user_data_dir"] = str(state / (venue + "-user-data"))
    if config != expected or config.get("dry_run") is not True:
        raise RuntimeValidationError("Runtime configuration differs from the dry-run template.")
    api = config.get("api_server", {})
    expected_api = {
        **template["api_server"],
        "enabled": True,
        "username": auth[venue]["username"],
        "password": auth[venue]["password"],
        "jwt_secret_key": api.get("jwt_secret_key"),
        "ws_token": api.get("ws_token"),
    }
    if api != expected_api or any(
        not isinstance(api.get(key), str) or len(api[key]) < 32
        for key in ("jwt_secret_key", "ws_token")
    ):
        raise RuntimeValidationError("Invalid loopback API configuration.")
    return config_path


def child_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FREQTRADE__") and key not in ALLOWED_NAMES
    }


def run(path: Path, venue: str, executable: str) -> None:
    state = state_directory(path)
    config = validated_config(state, venue)
    environment = child_environment()
    result = subprocess.run(
        [executable, "--version"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        result.returncode
        or re.search(rf"(?m)^Freqtrade Version:\s+freqtrade {re.escape(VERSION)}$", result.stdout)
        is None
    ):
        raise RuntimeValidationError(f"Install pinned Freqtrade {VERSION} from requirements.txt.")
    # No pass-through CLI arguments: live mode and arbitrary config overrides are unavailable.
    os.execvpe(
        executable,
        [
            executable,
            "trade",
            "--config",
            str(config),
            "--strategy",
            "CrossExchangeArb",
            "--strategy-path",
            str(HERE / "strategies"),
        ],
        environment,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparer = commands.add_parser("prepare", help="Generate private dry-run state and API auth.")
    preparer.add_argument("--state-dir", type=Path, default=ROOT / "state" / "freqtrade-arb")
    runner = commands.add_parser("run", help="Start one dry-run instance; never starts live mode.")
    runner.add_argument("venue", choices=VENUES)
    runner.add_argument("--state-dir", type=Path, default=ROOT / "state" / "freqtrade-arb")
    runner.add_argument("--freqtrade", default="freqtrade", help="Pinned executable path.")
    checker = commands.add_parser("credentials-check", help="Report names/presence, never values.")
    checker.add_argument("--zshrc", type=Path, default=Path.home() / ".zshrc")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            auth = prepare(args.state_dir)
            print(json.dumps({"mode": "dry_run", "api_auth_file": str(auth)}))
        elif args.command == "credentials-check":
            print(json.dumps(credential_presence(args.zshrc), indent=2))
        else:
            run(args.state_dir, args.venue, args.freqtrade)
    except RuntimeValidationError as exc:
        parser.exit(2, f"Error: {exc}\n")
    except (OSError, UnicodeError):
        parser.exit(2, "Error: could not access runtime input; no credential contents displayed.\n")


if __name__ == "__main__":
    main()
