"""Pilot tests: the `/` filter and `f` prune the sidebar too, not just the table."""

import asyncio

import pytest
from textual.widgets import DataTable, Input, Tree

from ftui import config
from ftui.app import FTUI
from ftui.fleet_screen import Scope, build_fleet_screen

ALL_APPS = {"acme-web", "acme-worker", "acme-legacy-cron", "acme-web-staging",
            "globex-api", "globex-db", "tps-reports"}


@pytest.fixture(autouse=True)
def no_accounts_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_PATH", tmp_path / "no-such-accounts.toml")
    monkeypatch.chdir(tmp_path)


async def loaded(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def nav_apps(screen):
    return {s.app for s in screen._tree_nodes if s.app}


def nav_accounts(screen):
    return {s.account for s in screen._tree_nodes if s.account and not s.org}


def label(screen, **scope):
    return str(screen._tree_nodes[Scope(**scope)].label)


async def set_filter(pilot, screen, text):
    screen.query_one("#filter", Input).value = text
    await pilot.pause()


def run(test):
    async def go():
        screen = build_fleet_screen(mock=True, refresh_interval=60)
        app = FTUI(refresh_interval=60, fleet_screen=screen)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(pilot)
            await test(pilot, screen)

    asyncio.run(go())


def test_filter_prunes_nav_to_matching_machines():
    async def t(pilot, screen):
        assert nav_apps(screen) == ALL_APPS
        await set_filter(pilot, screen, "state:stopped region:ams")
        assert nav_apps(screen) == {"acme-web-staging"}
        assert nav_accounts(screen) == {"acme"}
        assert {s.org for s in screen._tree_nodes if s.org and not s.app} == {"acme-staging"}
        # matched/total where counts already appear
        assert label(screen, account="acme", org="acme-staging", app="acme-web-staging") \
            .startswith("acme-web-staging 1/1 ")
        assert label(screen, account="acme").startswith("acme 1/5 ")
        assert str(screen.query_one(Tree).root.label).startswith("All 1/10 ")
        assert "1/10 machines, 1/7 apps, 1/3 accounts" in pilot.app.title

        # clearing the filter restores everything, with plain counts
        await set_filter(pilot, screen, "")
        assert nav_apps(screen) == ALL_APPS
        assert nav_accounts(screen) == {"acme", "globex", "initech"}
        assert label(screen, account="acme", org="acme-prod", app="acme-web") \
            .startswith("acme-web 3 ")
        assert "10/10 machines, 7 apps, 3 accounts" in pilot.app.title

    run(t)


def test_name_terms_keep_apps_without_machines():
    async def t(pilot, screen):
        # acme-legacy-cron has no machines (it fails to load) but its name matches
        for text in ("app:acme-legacy-cron", "cron", "app:*-cron"):
            await set_filter(pilot, screen, text)
            assert nav_apps(screen) == {"acme-legacy-cron"}, text
            assert screen.shown == []

        # org: and acct: prune by name too, and include its 0-machine apps
        await set_filter(pilot, screen, "acct:acme")
        assert nav_apps(screen) == {"acme-web", "acme-worker", "acme-legacy-cron",
                                    "acme-web-staging"}
        await set_filter(pilot, screen, "org:globex")
        assert nav_apps(screen) == {"globex-api", "globex-db"}
        assert nav_accounts(screen) == {"globex"}

        # negation
        await set_filter(pilot, screen, "-app:*-staging")
        assert nav_apps(screen) == ALL_APPS - {"acme-web-staging"}

        # machine terms never match by name: the 0-machine app goes
        await set_filter(pilot, screen, "acct:acme state:stopped")
        assert nav_apps(screen) == {"acme-web", "acme-web-staging"}

    run(t)


def test_f_cycle_prunes_nav():
    async def t(pilot, screen):
        screen.query_one(DataTable).focus()
        await pilot.press("f")
        assert screen.state_mode == "started"
        assert nav_apps(screen) == {"acme-web", "acme-worker", "globex-api", "globex-db"}
        assert label(screen, account="acme", org="acme-prod", app="acme-web") \
            .startswith("acme-web 2/3 ")
        await pilot.press("f")
        assert nav_apps(screen) == {"acme-web", "acme-web-staging", "globex-api", "tps-reports"}
        # f and the filter combine
        await set_filter(pilot, screen, "acct:initech")
        assert nav_apps(screen) == {"tps-reports"}
        await set_filter(pilot, screen, "")
        screen.query_one(DataTable).focus()
        await pilot.press("f")
        assert screen.state_mode == "all"
        assert nav_apps(screen) == ALL_APPS

    run(t)


def test_selection_kept_if_it_matches_else_first_match():
    async def t(pilot, screen):
        tree = screen.query_one(Tree)
        web = Scope("acme", "acme-prod", "acme-web")
        tree.select_node(screen._tree_nodes[web])
        await pilot.pause()
        assert screen.scope == web

        # still matches: kept
        await set_filter(pilot, screen, "region:fra")
        assert screen.scope == web
        assert [m.id for m in screen.shown] == ["7811d615b04489"]
        assert tree.cursor_node is screen._tree_nodes[web]

        # no longer matches: the first matching app, with machines, is selected
        await set_filter(pilot, screen, "acct:initech")
        assert screen.scope == Scope("initech", "initech", "tps-reports")
        assert len(screen.shown) == 2
        await pilot.pause()
        assert tree.cursor_node is screen._tree_nodes[screen.scope]

        # nothing matches at all: back to All, empty table
        await set_filter(pilot, screen, "no-such-thing")
        assert nav_apps(screen) == set()
        assert screen.scope == Scope() and screen.shown == []

        # clearing brings back the user's own selection
        await set_filter(pilot, screen, "")
        assert screen.scope == web
        assert len(screen.shown) == 3

    run(t)


def test_filter_keys_unchanged_and_table_still_filters():
    async def t(pilot, screen):
        await pilot.press("slash")
        assert isinstance(pilot.app.focused, Input)
        for ch in "app:globex-*":
            await pilot.press(ch)
        await pilot.press("enter")
        assert isinstance(pilot.app.focused, DataTable)
        assert {m.app for m in screen.shown} == {"globex-api", "globex-db"}
        assert nav_apps(screen) == {"globex-api", "globex-db"}

    run(t)
