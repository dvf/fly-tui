"""Accounts configuration: which Fly accounts ftui shows, and where their tokens live.

Tokens are never stored in the config file itself, only a reference to where
the token can be read from:

    fly               the local flyctl login (`fly auth token`)
    env:NAME          an environment variable
    op://vault/item   the 1Password CLI (`op read`)
    keychain:service  the macOS Keychain (`security find-generic-password -s service -w`)
"""

import fnmatch
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_PATH = Path(os.path.expanduser("~/.config/ftui/accounts.toml"))


class ConfigError(Exception):
    """The accounts file is invalid, or a token could not be read."""
    pass


@dataclass
class Account:
    """A Fly account ftui can see, with the token it reaches it by."""
    name: str
    token_source: str = "fly"
    orgs: Tuple[str, ...] = ()
    read_only: bool = False
    # Shell-style app name patterns ("shop-*"). Empty `apps` means every app.
    apps: Tuple[str, ...] = ()
    exclude_apps: Tuple[str, ...] = ()
    # Never shown in reprs, logs or errors.
    token: Optional[str] = field(default=None, repr=False, compare=False)

    def shows_app(self, name: str) -> bool:
        """Whether this account's `apps` / `exclude_apps` patterns admit `name`."""
        if self.apps and not any(fnmatch.fnmatchcase(name, p) for p in self.apps):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in self.exclude_apps)

    @property
    def uses_local_login(self) -> bool:
        return self.token_source == "fly"

    def flyctl_env(self) -> dict:
        """Environment for a flyctl subprocess acting as this account."""
        env = dict(os.environ)
        if self.token and not self.uses_local_login:
            env["FLY_API_TOKEN"] = self.token
        return env


def _run(cmd: List[str], what: str) -> str:
    if not shutil.which(cmd[0]):
        raise ConfigError(f"{what}: `{cmd[0]}` not found in PATH")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise ConfigError(f"{what}: `{cmd[0]}` timed out")
    if proc.returncode != 0:
        # stderr from these tools never contains the secret itself.
        raise ConfigError(f"{what}: {proc.stderr.strip() or 'lookup failed'}")
    return proc.stdout.strip()


def resolve_token(source: str) -> str:
    """Read a token from its source. Raises ConfigError, never echoing the token."""
    if source == "fly":
        fly = shutil.which("fly") or shutil.which("flyctl") or "fly"
        token = _run([fly, "auth", "token"], "flyctl login")
    elif source.startswith("env:"):
        name = source[4:]
        token = os.environ.get(name, "")
        if not token:
            raise ConfigError(f"environment variable {name} is not set")
    elif source.startswith("op://"):
        token = _run(["op", "read", source], "1Password")
    elif source.startswith("keychain:"):
        service = source[len("keychain:"):]
        token = _run(
            ["security", "find-generic-password", "-s", service, "-w"], "Keychain"
        )
    else:
        raise ConfigError(
            "token must be 'fly', 'env:NAME', 'op://...' or 'keychain:NAME' "
            "(raw tokens are not accepted in the config file)"
        )
    token = token.strip()
    if not token:
        raise ConfigError(f"empty token from {source.split(':')[0]}")
    return token


def parse_accounts(text: str) -> List[Account]:
    """Parse the accounts TOML text into Account records (tokens unresolved)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML: {e}")

    accounts = []
    seen = set()
    for i, raw in enumerate(data.get("account", [])):
        name = raw.get("name")
        if not name:
            raise ConfigError(f"account #{i + 1} has no name")
        if name in seen:
            raise ConfigError(f"duplicate account name: {name}")
        seen.add(name)
        orgs = raw.get("orgs", [])
        if isinstance(orgs, str):
            orgs = [orgs]
        patterns = {}
        for key in ("apps", "exclude_apps"):
            value = raw.get(key, [])
            patterns[key] = tuple([value] if isinstance(value, str) else value)
        accounts.append(Account(
            name=name,
            token_source=raw.get("token", "fly"),
            orgs=tuple(orgs),
            read_only=bool(raw.get("read_only", False)),
            **patterns,
        ))
    if not accounts:
        raise ConfigError("no [[account]] entries")
    return accounts


def load_accounts(path: Optional[Path] = None) -> List[Account]:
    """Load accounts from `path` (default ~/.config/ftui/accounts.toml).

    With no config file, returns one implicit account using the local flyctl login.
    """
    explicit = path is not None
    path = Path(path) if path else DEFAULT_PATH
    if not path.exists():
        if explicit:
            raise ConfigError(f"accounts file not found: {path}")
        return [Account(name="local", token_source="fly")]
    return parse_accounts(path.read_text())


def read_fly_toml_app(directory: Optional[Path] = None) -> Optional[str]:
    """Return the `app` name from fly.toml in `directory` (default cwd), if any."""
    path = Path(directory or os.getcwd()) / "fly.toml"
    try:
        return tomllib.loads(path.read_text()).get("app") or None
    except (OSError, tomllib.TOMLDecodeError):
        return None
