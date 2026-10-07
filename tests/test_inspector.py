"""Inspect panel logic: TOML rendering, secret parsing, diffs, sibling apps."""

import asyncio
import dataclasses
import json
import tomllib

import pytest

from ftui import inspector as inspector_mod
from ftui.client import FlyError
from ftui.config import Account
from ftui.fleet import Machine
from ftui.inspector import (
    AppChoice, Inspector, Secret, diff_env, diff_secrets, machine_env, parse_secrets,
    sibling_app, sibling_names, to_toml,
)

DECOY = "decoy-secret-value-must-never-render"


# -- secrets ------------------------------------------------------------------------

def test_parse_secrets_keeps_metadata_only():
    raw = json.dumps([
        {"name": "DATABASE_URL", "digest": "a1f0c3d9e2b47781", "status": "Deployed",
         "value": DECOY, "Value": DECOY, "secret": DECOY},
        {"Name": "API_KEY", "Digest": "6d0b4f9e2a7c1583", "Status": "Staged",
         "CreatedAt": "2026-09-01T10:00:00Z"},
        {"digest": "no-name-is-skipped"},
    ])
    secrets = parse_secrets(raw)
    assert [s.name for s in secrets] == ["API_KEY", "DATABASE_URL"]
    assert secrets[0] == Secret("API_KEY", "6d0b4f9e2a7c1583", "Staged", "2026-09-01 10:00:00")
    assert secrets[1].status == "Deployed"
    # there is no field a value could land in
    assert {f.name for f in dataclasses.fields(Secret)} == {"name", "digest", "status", "created_at"}
    assert DECOY not in repr(secrets)


def test_parse_secrets_graphql_shape_and_empty():
    nodes = {"nodes": [{"name": "A", "digest": "d1", "createdAt": "2026-01-02T03:04:05Z"}]}
    assert parse_secrets(nodes) == [Secret("A", "d1", "", "2026-01-02 03:04:05")]
    assert parse_secrets("") == [] and parse_secrets("[]") == []


# -- TOML -----------------------------------------------------------------------------

def test_to_toml_round_trips():
    config = {
        "app": "shop-api",
        "primary_region": "fra",
        "kill_timeout": 5,
        "swap_size_mb": 0.5,
        "console_command": None,
        "build": {"image": "registry.fly.io/shop-api:v1"},
        "env": {"LOG_LEVEL": "info", "weird key": 'quo"te\nnewline', "EMPTY": ""},
        "http_service": {
            "internal_port": 8080, "force_https": True, "processes": ["app"],
            "concurrency": {"type": "requests", "soft_limit": 20},
            "checks": [{"path": "/health", "interval": "30s"}],
        },
        "vm": [{"size": "shared-cpu-1x"}, {"size": "performance-2x", "processes": ["worker"]}],
        "mounts": [],
        "statics": [{"guest_path": "/app/public", "url_prefix": "/static/"}],
        "metadata": {},
    }
    text = to_toml(config)
    assert 'app = "shop-api"' in text and "[[vm]]" in text and "[http_service.concurrency]" in text
    expected = {k: v for k, v in config.items() if v is not None}
    assert tomllib.loads(text) == expected


# -- diffs ------------------------------------------------------------------------------

def test_diff_env():
    a = {"APP_ENV": "production", "PORT": "8080", "ONLY_PROD": "1", "LOG": "info"}
    b = {"APP_ENV": "staging", "PORT": "8080", "ONLY_STAGING": "1", "LOG": "debug"}
    d = diff_env(a, b)
    assert d.differ == [("APP_ENV", "production", "staging"), ("LOG", "info", "debug")]
    assert d.only_a == ["ONLY_PROD"] and d.only_b == ["ONLY_STAGING"] and d.same == ["PORT"]
    assert diff_env({}, {}).differ == []


def test_diff_secrets_flags_same_digest():
    prod = [Secret("DATABASE_URL", "aaa"), Secret("PAYMENTS_API_KEY", "same"),
            Secret("MAILER_TOKEN", "m1"), Secret("NO_DIGEST", "")]
    staging = [Secret("DATABASE_URL", "bbb"), Secret("PAYMENTS_API_KEY", "same"),
               Secret("DEBUG_TOKEN", "d1"), Secret("NO_DIGEST", "")]
    d = diff_secrets(prod, staging)
    assert d.same_digest == ["PAYMENTS_API_KEY"]
    assert d.only_a == ["MAILER_TOKEN"] and d.only_b == ["DEBUG_TOKEN"]
    # empty digests are never reported as "same value"
    assert d.different == ["DATABASE_URL", "NO_DIGEST"]


def test_sibling_app():
    names = ["shop-api", "shop-api-staging", "globex-web", "acme-prod", "acme-staging"]
    assert sibling_app("shop-api", names) == "shop-api-staging"
    assert sibling_app("shop-api-staging", names) == "shop-api"
    assert sibling_app("acme-prod", names) == "acme-staging"
    assert sibling_app("acme-staging", names) == "acme-prod"
    assert sibling_app("globex-web", names) is None
    assert "globex-web" not in sibling_names("globex-web")


def test_machine_env_from_api():
    raw = {"id": "m1", "config": {"env": {"B": "2", "A": 1, "N": None}}}
    assert machine_env(raw) == {"A": "1", "B": "2", "N": ""}
    m = Machine.from_api("acme", "acme", "shop-api", raw)
    assert m.env == (("A", "1"), ("B", "2"), ("N", ""))
    assert machine_env({}) == {}


# -- flyctl calls ---------------------------------------------------------------------

class FakeProc:
    def __init__(self, out: str = "", err: str = "", code: int = 0):
        self.out, self.err, self.returncode = out.encode(), err.encode(), code

    async def communicate(self):
        return self.out, self.err


@pytest.fixture
def fake_flyctl(monkeypatch):
    calls = []
    responses = {}

    async def exec_(*cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        return responses.get(cmd[1:3], FakeProc(err="Error: unexpected command", code=1))

    monkeypatch.setattr(inspector_mod.asyncio, "create_subprocess_exec", exec_)
    return calls, responses


def test_inspector_runs_read_only_commands_as_the_account(fake_flyctl):
    calls, responses = fake_flyctl
    responses[("secrets", "list")] = FakeProc(json.dumps(
        [{"name": "API_KEY", "digest": "d1", "status": "Deployed", "value": DECOY}]))
    responses[("config", "show")] = FakeProc('app = "shop-api"\n')
    account = Account(name="acme", token_source="env:X", token="fm2_not_a_real_token")
    insp = Inspector()
    insp._bin = "fly"
    choice = AppChoice("shop-api", account)

    secrets = asyncio.run(insp.secrets(choice))
    config = asyncio.run(insp.config_toml(choice))
    assert secrets == [Secret("API_KEY", "d1", "Deployed")]
    assert config == 'app = "shop-api"\n'
    cmds = [c for c, _ in calls]
    assert cmds[0] == ("fly", "secrets", "list", "--json", "-a", "shop-api")
    assert cmds[1] == ("fly", "config", "show", "--toml", "-a", "shop-api")
    assert all(env["FLY_API_TOKEN"] == "fm2_not_a_real_token" for _, env in calls)
    # never anything that could print a value
    assert not any(w in c for c in cmds for w in ("ssh", "printenv", "console", "set", "unset"))


def test_inspector_config_falls_back_to_json(fake_flyctl, monkeypatch):
    calls, _ = fake_flyctl

    async def exec_(*cmd, **kwargs):
        calls.append(cmd)
        if "--toml" in cmd:
            return FakeProc(err="Error: unknown flag: --toml", code=1)
        return FakeProc(json.dumps({"app": "shop-api", "vm": [{"size": "shared-cpu-1x"}]}))

    monkeypatch.setattr(inspector_mod.asyncio, "create_subprocess_exec", exec_)
    insp = Inspector()
    insp._bin = "fly"
    text = asyncio.run(insp.config_toml(AppChoice("shop-api")))
    assert tomllib.loads(text) == {"app": "shop-api", "vm": [{"size": "shared-cpu-1x"}]}


def test_inspector_errors(fake_flyctl):
    calls, responses = fake_flyctl
    responses[("secrets", "list")] = FakeProc(err="Error: Not authorized to access this app", code=1)
    insp = Inspector()
    insp._bin = "fly"
    with pytest.raises(FlyError, match="Not authorized"):
        asyncio.run(insp.secrets(AppChoice("")))
    # no -a without an app name: flyctl uses ./fly.toml
    assert calls[0][0] == ("fly", "secrets", "list", "--json")

    insp._bin = None
    with pytest.raises(FlyError, match="flyctl not found"):
        asyncio.run(insp.config_toml(AppChoice("shop-api")))
