"""Read-only inspection of an app: its deployed config, a machine's env, its
secret names and digests, and a diff of all that against a second app.

Nothing here changes anything, so read-only accounts can use it too.

Secret values are never fetched, displayed or logged. `fly secrets list`
returns names, digests and deployment status only, and `parse_secrets` keeps
just those fields: if a value-like field ever appeared in the output it would
be dropped before reaching the UI. There is no printenv or ssh path.
"""

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ftui.client import FlyError
from ftui.config import Account

STAGING = "-staging"
# Suffixes treated as "the production twin" when looking for a staging sibling.
PROD_SUFFIXES = ("-production", "-prod")


# -- data -------------------------------------------------------------------------

@dataclass(frozen=True)
class Secret:
    """A secret's metadata. There is deliberately no field for its value."""
    name: str
    digest: str = ""
    status: str = ""
    created_at: str = ""


def _pick(raw: dict, *keys: str) -> str:
    for key in keys:
        if raw.get(key) not in (None, ""):
            return str(raw[key])
    return ""


def parse_secrets(data) -> List[Secret]:
    """Secrets from `fly secrets list --json` (or GraphQL `secrets`) output.

    Only name, digest, status and created time are kept; anything else in the
    input, including any value-like field, is ignored.
    """
    if isinstance(data, str):
        data = json.loads(data) if data.strip() else []
    if isinstance(data, dict):  # GraphQL-style {"nodes": [...]}
        data = data.get("nodes") or data.get("secrets") or []
    secrets = []
    for raw in data or []:
        if not isinstance(raw, dict):
            continue
        name = _pick(raw, "name", "Name")
        if not name:
            continue
        secrets.append(Secret(
            name=name,
            digest=_pick(raw, "digest", "Digest"),
            status=_pick(raw, "status", "Status"),
            created_at=_pick(raw, "created_at", "createdAt", "CreatedAt")[:19].replace("T", " "),
        ))
    return sorted(secrets, key=lambda s: s.name)


def machine_env(raw_machine: dict) -> Dict[str, str]:
    """The `config.env` of a machine from the Machines API / `fly machines list --json`."""
    env = ((raw_machine or {}).get("config") or {}).get("env") or {}
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


# -- TOML rendering -------------------------------------------------------------

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key) -> str:
    key = str(key)
    return key if _BARE_KEY.match(key) else _toml_str(key)


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, dict):
        items = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items() if v is not None]
        return "{ " + ", ".join(items) + " }" if items else "{}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value if v is not None) + "]"
    return _toml_str(str(value))


def _is_table_array(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _emit_table(table: dict, path: List[str], lines: List[str]) -> None:
    for key, value in table.items():
        if value is None or isinstance(value, dict) or _is_table_array(value):
            continue
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in table.items():
        sub = path + [_toml_key(key)]
        if isinstance(value, dict):
            lines += ["", f"[{'.'.join(sub)}]"]
            _emit_table(value, sub, lines)
        elif _is_table_array(value):
            for item in value:
                lines += ["", f"[[{'.'.join(sub)}]]"]
                _emit_table(item, sub, lines)


def to_toml(data: dict) -> str:
    """Render a JSON-like dict (e.g. `fly config show` output) as TOML."""
    lines: List[str] = []
    _emit_table(data or {}, [], lines)
    return "\n".join(lines).strip() + "\n"


# -- diffs --------------------------------------------------------------------------

@dataclass
class EnvDiff:
    differ: List[Tuple[str, str, str]] = field(default_factory=list)  # (key, a, b)
    only_a: List[str] = field(default_factory=list)
    only_b: List[str] = field(default_factory=list)
    same: List[str] = field(default_factory=list)


def diff_env(a: Dict[str, str], b: Dict[str, str]) -> EnvDiff:
    """Env keys that differ between two apps, or exist on one side only."""
    d = EnvDiff()
    for key in sorted(set(a) | set(b)):
        if key not in b:
            d.only_a.append(key)
        elif key not in a:
            d.only_b.append(key)
        elif a[key] != b[key]:
            d.differ.append((key, a[key], b[key]))
        else:
            d.same.append(key)
    return d


@dataclass
class SecretsDiff:
    same_digest: List[str] = field(default_factory=list)  # same value on both: the red flag
    only_a: List[str] = field(default_factory=list)
    only_b: List[str] = field(default_factory=list)
    different: List[str] = field(default_factory=list)


def diff_secrets(a: Iterable[Secret], b: Iterable[Secret]) -> SecretsDiff:
    """Compare two apps' secrets by name and digest (never by value).

    A secret with the same digest on both sides holds the same value on both,
    e.g. production running with a staging key.
    """
    da = {s.name: s.digest for s in a}
    db = {s.name: s.digest for s in b}
    d = SecretsDiff()
    for name in sorted(set(da) | set(db)):
        if name not in db:
            d.only_a.append(name)
        elif name not in da:
            d.only_b.append(name)
        elif da[name] and da[name] == db[name]:
            d.same_digest.append(name)
        else:
            d.different.append(name)
    return d


def sibling_names(name: str) -> List[str]:
    """Likely prod/staging twins of `name`, most likely first."""
    out = []
    if name.endswith(STAGING):
        base = name[: -len(STAGING)]
        out.append(base)
        out += [base + s for s in PROD_SUFFIXES]
    else:
        out.append(name + STAGING)
        for suffix in PROD_SUFFIXES:
            if name.endswith(suffix):
                out.append(name[: -len(suffix)] + STAGING)
    return [n for n in out if n and n != name]


def sibling_app(name: str, names: Iterable[str]) -> Optional[str]:
    """The app whose name adds or removes a `-staging` suffix, if one exists."""
    names = set(names)
    return next((n for n in sibling_names(name) if n in names), None)


# -- fetching -----------------------------------------------------------------------

@dataclass
class AppChoice:
    """An app to inspect, and the account to read it as (None: the local login)."""
    app: str
    account: Optional[Account] = None

    @property
    def label(self) -> str:
        name = self.app or "(fly.toml app)"
        return f"{name} ({self.account.name})" if self.account else name

    def same_as(self, other: "AppChoice") -> bool:
        return self.app == other.app and (
            (self.account.name if self.account else None)
            == (other.account.name if other.account else None)
        )


class Inspector:
    """Reads config, env and secret metadata with flyctl, as the app's account.

    Every command is read-only: `config show`, `secrets list`, `machines list`
    and `apps list`.
    """

    def __init__(self, mock: bool = False, timeout: float = 30.0):
        self.mock = mock
        self.timeout = timeout
        self._bin = shutil.which("fly") or shutil.which("flyctl")

    async def _flyctl(self, choice: AppChoice, *args: str) -> str:
        if not self._bin:
            raise FlyError("flyctl not found in PATH")
        if choice.app:
            args = (*args, "-a", choice.app)
        env = choice.account.flyctl_env() if choice.account else None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            if proc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise FlyError(f"timed out: fly {args[0]} {args[1]}")
        except OSError as e:
            raise FlyError(str(e))
        if proc.returncode != 0:
            message = stderr.decode().strip() or f"fly exited with code {proc.returncode}"
            raise FlyError(message.splitlines()[-1][:300])
        return stdout.decode()

    async def config_toml(self, choice: AppChoice) -> str:
        """The deployed app config, as TOML."""
        if self.mock:
            return to_toml(_mock(choice.app, "config"))
        try:
            return await self._flyctl(choice, "config", "show", "--toml")
        except FlyError as e:
            if "unknown flag" not in str(e):
                raise
        # older flyctl: JSON only
        text = await self._flyctl(choice, "config", "show")
        try:
            return to_toml(json.loads(text))
        except ValueError:
            return text

    async def secrets(self, choice: AppChoice) -> List[Secret]:
        """Secret names, digests and status. Never values."""
        if self.mock:
            return parse_secrets(_mock(choice.app, "secrets"))
        text = await self._flyctl(choice, "secrets", "list", "--json")
        try:
            return parse_secrets(text)
        except ValueError:
            raise FlyError("could not parse `fly secrets list --json` output")

    async def machine_env(self, choice: AppChoice, process_group: str = "") -> Tuple[Dict[str, str], str]:
        """(env, machine id) of one of the app's machines, preferring `process_group`."""
        if self.mock:
            machines = _mock(choice.app, "machines")
        else:
            text = await self._flyctl(choice, "machines", "list", "--json")
            try:
                machines = json.loads(text) if text.strip() else []
            except ValueError:
                raise FlyError("could not parse `fly machines list --json` output")
        if not machines:
            raise FlyError(f"{choice.app or 'app'} has no machines")

        def group(m: dict) -> str:
            return ((m.get("config") or {}).get("metadata") or {}).get("fly_process_group", "")

        chosen = next((m for m in machines if process_group and group(m) == process_group), machines[0])
        return machine_env(chosen), chosen.get("id", "")

    async def list_apps(self, account: Optional[Account] = None) -> List[str]:
        """App names the account can see (for picking a diff target)."""
        if self.mock:
            from ftui.mock import MOCK_SINGLE_APPS
            return list(MOCK_SINGLE_APPS)
        text = await self._flyctl(AppChoice("", account), "apps", "list", "--json")
        try:
            data = json.loads(text) if text.strip() else []
        except ValueError:
            raise FlyError("could not parse `fly apps list --json` output")
        return sorted(a.get("Name") or a.get("name") for a in data if a.get("Name") or a.get("name"))


def _mock(app: str, what: str):
    from ftui.mock import mock_inspect
    return mock_inspect(app, what)
