"""Pilot tests: the inspect panel (`c`) in mock mode, in both views."""

import asyncio
import io

import pytest
from rich.console import Console
from textual.widgets import DataTable, Static, TabbedContent

from ftui import config
from ftui import mock as mock_mod
from ftui.app import FTUI, MachineListScreen
from ftui.fleet_screen import build_fleet_screen
from ftui.inspect_screen import SAME_VALUE, AppPicker, InspectScreen

DECOY = "decoy-secret-value-must-never-render"


@pytest.fixture(autouse=True)
def no_accounts_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_PATH", tmp_path / "no-such-accounts.toml")


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def plain(renderable) -> str:
    console = Console(file=io.StringIO(), width=200, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def static_text(screen, selector) -> str:
    return plain(screen.query_one(selector, Static).content)


def table_rows(screen, selector):
    table = screen.query_one(selector, DataTable)
    return [[plain(c).strip() for c in table.get_row_at(i)] for i in range(table.row_count)]


def all_text(screen) -> str:
    """Everything the panel renders: every Static and every table cell."""
    parts = [plain(w.content) for w in screen.query(Static)]
    for table in screen.query(DataTable):
        parts += ["|".join(row) for row in table_rows(screen, f"#{table.id}")]
    return "\n".join(parts)


async def open_fleet_on(pilot, screen, app_name):
    await settle(pilot)
    node = next(n for s, n in screen._tree_nodes.items() if s.app == app_name)
    screen.query_one("#sidebar").select_node(node)
    await pilot.pause()
    screen.query_one("#machines-table", DataTable).focus()
    await pilot.press("c")
    await settle(pilot)
    assert isinstance(pilot.app.screen, InspectScreen)
    return pilot.app.screen


def test_fleet_inspect_panel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def go():
        screen = build_fleet_screen(mock=True, refresh_interval=60)
        app = FTUI(refresh_interval=60, fleet_screen=screen)
        async with app.run_test(size=(200, 50)) as pilot:
            panel = await open_fleet_on(pilot, screen, "acme-web")
            assert panel.target.app == "acme-web" and panel.target.account.name == "acme"

            # Config: the deployed config, as TOML
            cfg = static_text(panel, "#config-body")
            assert 'app = "acme-web"' in cfg and "[http_service]" in cfg and "[[vm]]" in cfg

            # Env: the machine's config.env, sorted
            env = table_rows(panel, "#env-table")
            assert [r[0] for r in env] == sorted(mock_mod.mock_env("acme-web"))
            assert ["APP_ENV", "production"] in env

            # Secrets: names, digests, status; no value column
            secrets = panel.query_one("#secrets-table", DataTable)
            assert [str(c.label) for c in secrets.columns.values()] == \
                ["Name", "Digest", "Status", "Created"]
            rows = table_rows(panel, "#secrets-table")
            assert ["PAYMENTS_API_KEY", "9c4e1b7a2d5f3086", "Deployed", "-"] in rows

            # Diff: defaults to the -staging sibling, flags same-digest secrets
            assert panel.diff_target.app == "acme-web-staging"
            diff = table_rows(panel, "#diff-table")
            assert ["secret", "PAYMENTS_API_KEY", SAME_VALUE, "same digest", "same digest"] in diff
            assert ["secret", "MAILER_TOKEN", "only in acme-web", "present", "-"] in diff
            assert ["secret", "DEBUG_TOOLBAR_TOKEN", "only in acme-web-staging", "-", "present"] in diff
            assert ["env", "APP_ENV", "differs", "production", "staging"] in diff
            assert ["env", "FEATURE_CHECKOUT_V2", "only in acme-web", "true", "-"] in diff
            assert ["env", "SEED_DEMO_DATA", "only in acme-web-staging", "-", "1"] in diff
            assert not any(r[1] == "PORT" for r in diff)  # same on both: not listed
            summary = static_text(panel, "#diff-msg")
            assert f"1 {SAME_VALUE}" in summary

            # number keys switch tabs
            await pilot.press("4")
            assert panel.query_one(TabbedContent).active == "tab-diff"

            # a: pick another app to diff against
            await pilot.press("a")
            await settle(pilot)
            assert isinstance(app.screen, AppPicker)
            for ch in "acme-worker":
                await pilot.press(ch)
            await pilot.press("enter")
            await settle(pilot)
            assert app.screen is panel and panel.diff_target.app == "acme-worker"
            diff = table_rows(panel, "#diff-table")
            assert ["secret", "DATABASE_URL", SAME_VALUE, "same digest", "same digest"] in diff

            # Esc closes the panel
            await pilot.press("escape")
            assert app.screen is screen

            # read-only accounts can inspect; q closes
            panel = await open_fleet_on(pilot, screen, "globex-api")
            assert panel.target.account.read_only
            assert "JWT_SIGNING_KEY" in [r[0] for r in table_rows(panel, "#secrets-table")]
            await pilot.press("q")
            assert app.screen is screen

            # errors stay in their tab: secrets fail, config and env still show
            panel = await open_fleet_on(pilot, screen, "globex-db")
            assert "Not authorized" in static_text(panel, "#secrets-msg")
            assert 'app = "globex-db"' in static_text(panel, "#config-body")
            assert table_rows(panel, "#env-table") == [["PRIMARY_REGION", "iad"]]
            await pilot.press("escape")

    asyncio.run(go())


def test_secret_values_never_render(tmp_path, monkeypatch):
    """Even if the secrets output carried a value field, the panel never shows it."""
    monkeypatch.chdir(tmp_path)
    real = mock_mod.mock_inspect

    def with_decoy(app, what):
        data = real(app, what)
        if what == "secrets":
            data = [dict(s, value=DECOY, Value=DECOY) for s in data]
        return data

    monkeypatch.setattr(mock_mod, "mock_inspect", with_decoy)

    async def go():
        screen = build_fleet_screen(mock=True, refresh_interval=60)
        app = FTUI(refresh_interval=60, fleet_screen=screen)
        async with app.run_test(size=(200, 50)) as pilot:
            panel = await open_fleet_on(pilot, screen, "acme-web")
            assert table_rows(panel, "#secrets-table")  # secrets did load
            assert table_rows(panel, "#diff-table")
            text = all_text(panel)
            assert "PAYMENTS_API_KEY" in text
            assert DECOY not in text
            for tab in ("tab-config", "tab-env", "tab-secrets", "tab-diff"):
                panel.query_one(TabbedContent).active = tab
                await pilot.pause()
                assert DECOY not in all_text(panel)

    asyncio.run(go())


def test_single_app_inspect_panel(monkeypatch):
    monkeypatch.setenv("FTUI_MOCK", "1")

    async def go():
        app = FTUI(refresh_interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.6)
            await settle(pilot)
            assert isinstance(app.screen, MachineListScreen)
            await pilot.press("c")
            await settle(pilot)
            panel = app.screen
            assert isinstance(panel, InspectScreen)
            assert panel.target.app == "mock-app-production" and panel.target.account is None
            assert 'app = "mock-app-production"' in static_text(panel, "#config-body")
            assert table_rows(panel, "#env-table")
            assert [r[0] for r in table_rows(panel, "#secrets-table")] == ["API_KEY", "DATABASE_URL"]
            assert panel.diff_target.app == "mock-app-staging"
            diff = table_rows(panel, "#diff-table")
            assert ["secret", "API_KEY", SAME_VALUE, "same digest", "same digest"] in diff
            await pilot.press("escape")
            assert isinstance(app.screen, MachineListScreen)

    asyncio.run(go())
