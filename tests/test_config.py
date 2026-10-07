import subprocess
from types import SimpleNamespace

import pytest

from ftui import config
from ftui.config import Account, ConfigError, load_accounts, parse_accounts, resolve_token

SECRET = "fm2_not_a_real_token"

TOML = """
[[account]]
name = "acme"
token = "fly"
orgs = ["acme-prod"]

[[account]]
name = "globex"
token = "env:FLY_TOKEN_GLOBEX"
read_only = true

[[account]]
name = "vault"
token = "op://Vault/Fly Globex/token"

[[account]]
name = "kc"
token = "keychain:fly-globex"
"""


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=SECRET + "\n", stderr="")

    monkeypatch.setattr(config.subprocess, "run", run)
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_parse_accounts():
    accounts = parse_accounts(TOML)
    assert [a.name for a in accounts] == ["acme", "globex", "vault", "kc"]
    assert accounts[0].orgs == ("acme-prod",)
    assert accounts[0].uses_local_login
    assert accounts[1].read_only is True
    assert accounts[2].token_source == "op://Vault/Fly Globex/token"


def test_parse_rejects_bad_config():
    with pytest.raises(ConfigError):
        parse_accounts("[[account]]\ntoken = 'fly'\n")  # no name
    with pytest.raises(ConfigError):
        parse_accounts("[[account]]\nname='a'\n[[account]]\nname='a'\n")
    with pytest.raises(ConfigError):
        parse_accounts("")


def test_token_from_flyctl(fake_run):
    assert resolve_token("fly") == SECRET
    assert fake_run[0][1:] == ["auth", "token"]


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("FLY_TOKEN_GLOBEX", SECRET)
    assert resolve_token("env:FLY_TOKEN_GLOBEX") == SECRET
    monkeypatch.delenv("FLY_TOKEN_GLOBEX")
    with pytest.raises(ConfigError, match="FLY_TOKEN_GLOBEX"):
        resolve_token("env:FLY_TOKEN_GLOBEX")


def test_token_from_1password(fake_run):
    assert resolve_token("op://Vault/Fly Globex/token") == SECRET
    assert fake_run[0] == ["op", "read", "op://Vault/Fly Globex/token"]


def test_token_from_keychain(fake_run):
    assert resolve_token("keychain:fly-globex") == SECRET
    assert fake_run[0] == ["security", "find-generic-password", "-s", "fly-globex", "-w"]


def test_missing_tool_and_raw_tokens_rejected(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    with pytest.raises(ConfigError, match="not found"):
        resolve_token("op://Vault/x/token")
    with pytest.raises(ConfigError, match="raw tokens"):
        resolve_token(SECRET)


def test_lookup_failure_does_not_leak(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        config.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=44, stdout="", stderr="item not found"),
    )
    with pytest.raises(ConfigError) as e:
        resolve_token("keychain:nope")
    assert "item not found" in str(e.value)


def test_token_never_in_repr():
    a = Account(name="x", token_source="env:X", token=SECRET)
    assert SECRET not in repr(a)


def test_flyctl_env_only_for_non_local_accounts():
    local = Account(name="l", token_source="fly", token=SECRET)
    remote = Account(name="r", token_source="env:X", token=SECRET)
    assert local.flyctl_env().get("FLY_API_TOKEN") != SECRET
    assert remote.flyctl_env()["FLY_API_TOKEN"] == SECRET


def test_no_config_file_means_local_login(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_PATH", tmp_path / "missing.toml")
    [account] = load_accounts()
    assert account.uses_local_login
    with pytest.raises(ConfigError):
        load_accounts(tmp_path / "missing.toml")  # an explicit --accounts must exist


def test_fly_toml_is_optional(tmp_path):
    assert config.read_fly_toml_app(tmp_path) is None
    (tmp_path / "fly.toml").write_text("not [valid toml")
    assert config.read_fly_toml_app(tmp_path) is None
    (tmp_path / "fly.toml").write_text('app = "acme-web"\n')
    assert config.read_fly_toml_app(tmp_path) == "acme-web"


def test_app_patterns_split_one_org_into_two_accounts():
    accounts = parse_accounts(
        '[[account]]\nname = "acme-shop"\norgs = ["acme"]\napps = ["shop-*"]\n'
        '[[account]]\nname = "acme"\norgs = "acme"\nexclude_apps = "shop-*"\n'
    )
    shop, rest = accounts
    assert shop.shows_app("shop-api-staging") and not shop.shows_app("blog")
    assert rest.shows_app("blog") and not rest.shows_app("shop-web")


def test_no_app_patterns_shows_every_app():
    (account,) = parse_accounts('[[account]]\nname = "a"\n')
    assert account.shows_app("anything")
