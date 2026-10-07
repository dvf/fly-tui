"""Pilot tests: the unchanged single-app view, and the multi-account view."""

import asyncio

import pytest
from textual.widgets import DataTable, Input, Tree

from ftui import config
from ftui.app import FTUI, LogScreen, MachineListScreen, ScaleDialog
from ftui.fleet_screen import ConfirmDialog, FleetScreen, build_fleet_screen
from ftui.main import initial_filter, use_fleet_view

EXISTING_KEYS = {"r", "l", "s", "h", "ctrl+s", "ctrl+x", "ctrl+r"}
INSPECT_KEY = "c"  # added by the inspect panel; the only new single-app key


def keys(screen_cls):
    return {b.key for b in screen_cls.BINDINGS}


@pytest.fixture(autouse=True)
def no_accounts_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_PATH", tmp_path / "no-such-accounts.toml")


# -- the existing single-app view: regression tests ---------------------------

def test_existing_keybindings_unchanged():
    assert keys(MachineListScreen) == EXISTING_KEYS | {INSPECT_KEY}
    assert keys(LogScreen) == {"q", "c", "ctrl+c"}


def test_fleet_keys_extend_without_collisions():
    fleet = keys(FleetScreen)
    assert EXISTING_KEYS <= fleet
    new = fleet - EXISTING_KEYS
    assert new == {"slash", "f", INSPECT_KEY}
    # `c` is Clear on the log screen only, which is a different screen
    assert not new & (EXISTING_KEYS | {"q", "ctrl+c"})


def test_fly_toml_without_config_keeps_single_app_view(tmp_path):
    (tmp_path / "fly.toml").write_text('app = "acme-web"\n')
    assert use_fleet_view(None, False, False, cwd=tmp_path) is False


def test_multi_view_when_no_fly_toml(tmp_path):
    assert use_fleet_view(None, False, False, cwd=tmp_path) is True
    assert initial_filter(None, None, None, cwd=tmp_path) == ""


def test_config_turns_fly_toml_into_starting_filter(tmp_path, monkeypatch):
    (tmp_path / "fly.toml").write_text('app = "acme-web"\n')
    accounts = tmp_path / "accounts.toml"
    accounts.write_text('[[account]]\nname = "a"\n')
    monkeypatch.setattr(config, "DEFAULT_PATH", accounts)
    assert use_fleet_view(None, False, False, cwd=tmp_path) is True
    assert initial_filter(None, None, None, cwd=tmp_path) == "app:acme-web"
    assert initial_filter(None, None, None, use_fly_toml=False, cwd=tmp_path) == ""


def test_flags_choose_multi_view_and_filter(tmp_path):
    (tmp_path / "fly.toml").write_text('app = "acme-web"\n')
    assert use_fleet_view(None, True, False, cwd=tmp_path) is True
    assert use_fleet_view(None, False, True, cwd=tmp_path) is True
    assert initial_filter("web", "acme-prod", "acme", cwd=tmp_path) == \
        "acct:acme org:acme-prod app:web"


def test_single_app_mock_mode_unchanged(monkeypatch):
    monkeypatch.setenv("FTUI_MOCK", "1")

    async def go():
        app = FTUI(refresh_interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.6)
            assert isinstance(app.screen, MachineListScreen)
            table = app.screen.query_one(DataTable)
            ids = [table.get_row_at(i)[1] for i in range(table.row_count)]
            assert ids == ["7811d615b04489", "148ed106c62289"]
            assert app.app_name == "mock-app-production"
            await pilot.press("s")
            assert isinstance(app.screen, ScaleDialog)

    asyncio.run(go())


# -- the multi-account view -----------------------------------------------------

async def loaded(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def rows(screen):
    table = screen.query_one(DataTable)
    return [row.key.value for row in table.ordered_rows]


def test_mock_fleet_in_dir_without_fly_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert use_fleet_view(None, False, False) is True

    async def go():
        screen = build_fleet_screen(mock=True, refresh_interval=60,
                                    initial_filter=initial_filter(None, None, None))
        app = FTUI(refresh_interval=60, fleet_screen=screen)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(pilot)
            assert app.screen is screen

            # every machine across 3 accounts, 4 orgs, 7 apps
            assert len(rows(screen)) == 10
            assert {k.split("/")[0] for k in rows(screen)} == {"acme", "globex", "initech"}
            assert len(screen.apps) == 7
            assert set(screen.app_errors) == {("acme", "acme-legacy-cron")}

            # sidebar: account -> org -> app, with counts, error and warning markers
            tree = screen.query_one(Tree)
            labels = {str(n.label) for n in screen._tree_nodes.values()}
            assert any(l.startswith("acme-web 3 ") and l.endswith("⚠") for l in labels)
            assert any(l.startswith("acme-legacy-cron ✖") for l in labels)
            assert any(l.startswith("globex ro") for l in labels)

            # filter bar
            await pilot.press("slash")
            assert isinstance(app.focused, Input)
            for ch in "state:stopped region:ams":
                await pilot.press("space" if ch == " " else ch)
            await pilot.press("enter")
            assert rows(screen) == ["acme/acme-web-staging/e2865d3b7c9f01"]
            screen.query_one(Input).value = ""
            await pilot.pause()
            assert len(rows(screen)) == 10

            # f cycles all -> started -> stopped -> all
            await pilot.press("f")
            assert all(m.state == "started" for m in screen.shown) and len(screen.shown) == 5
            await pilot.press("f")
            assert all(m.state == "stopped" for m in screen.shown) and len(screen.shown) == 5
            await pilot.press("f")
            assert len(screen.shown) == 10

            # selecting a tree node scopes the table
            node = next(n for s, n in screen._tree_nodes.items() if s.app == "tps-reports")
            tree.select_node(node)
            await pilot.pause()
            assert [m.app for m in screen.shown] == ["tps-reports", "tps-reports"]

            # cursor stays on the same machine across a refresh
            table = screen.query_one(DataTable)
            table.move_cursor(row=1)
            before = screen.selected_key()
            await screen.refresh_data()
            assert screen.selected_key() == before

            # a mutating action asks first, naming account/org/app/machine
            await pilot.press("ctrl+x")
            assert isinstance(app.screen, ConfirmDialog)
            assert "account: initech" in app.screen.target and "tps-reports" in app.screen.target
            await pilot.press("n")
            assert app.screen is screen

            # start, confirmed, lands in the mock fleet for that app
            await pilot.press("ctrl+s")
            await pilot.press("y")
            await loaded(pilot)
            started = [m for m in screen.shown if m.key == before]
            assert started and started[0].state == "started"

            # read-only accounts refuse mutating actions without a dialog
            node = next(n for s, n in screen._tree_nodes.items() if s.app == "globex-api")
            tree.select_node(node)
            await pilot.pause()
            await pilot.press("ctrl+x")
            assert app.screen is screen

    asyncio.run(go())


def test_mock_fleet_starts_filtered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def go():
        screen = build_fleet_screen(mock=True, refresh_interval=60,
                                    initial_filter=initial_filter(None, "globex", None))
        app = FTUI(refresh_interval=60, fleet_screen=screen)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(pilot)
            assert {m.org for m in screen.shown} == {"globex"}
            assert len(screen.shown) == 3

    asyncio.run(go())
