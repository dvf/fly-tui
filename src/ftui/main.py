import click
import os
import sys
from pathlib import Path
from typing import Optional

from ftui.app import FTUI
from ftui import config
from ftui.config import ConfigError, load_accounts, read_fly_toml_app
from ftui.fly_client import FlyClient  # noqa: F401  (kept for compatibility)


def use_fleet_view(accounts_path: Optional[Path], filters: bool, show_all: bool,
                   cwd: Optional[Path] = None) -> bool:
    """Pick the multi-account view, or today's single-app view.

    The single-app view is kept whenever it worked before: a fly.toml in the
    current directory and no accounts config or multi-account flags.
    """
    if accounts_path or filters or show_all:
        return True
    if config.DEFAULT_PATH.exists():
        return True
    return not (Path(cwd or os.getcwd()) / "fly.toml").exists()


def initial_filter(app: Optional[str], org: Optional[str], account: Optional[str],
                   use_fly_toml: bool = True, cwd: Optional[Path] = None) -> str:
    """Filter-bar text to start with: explicit flags, else the local fly.toml app."""
    tokens = [f"acct:{account}" if account else "", f"org:{org}" if org else "",
              f"app:{app}" if app else ""]
    tokens = [t for t in tokens if t]
    if not tokens and use_fly_toml:
        # fly.toml is only a convenience starting filter; it is never required.
        if detected := read_fly_toml_app(cwd):
            tokens = [f"app:{detected}"]
    return " ".join(tokens)


@click.command()
@click.option("--mock", is_flag=True, help="Use mock flyctl for development")
@click.option("--refresh", default=5, type=int, help="Refresh interval in seconds (default: 5)")
@click.option("--accounts", "accounts_path", type=click.Path(dir_okay=False, path_type=Path),
              help="Accounts file for the multi-account view "
                   "(default: ~/.config/ftui/accounts.toml)")
@click.option("--app", help="Multi-account view, starting filtered to this app (glob)")
@click.option("--org", help="Multi-account view, starting filtered to this org (glob)")
@click.option("--account", help="Multi-account view, starting filtered to this account (glob)")
@click.option("--all", "show_all", is_flag=True,
              help="Multi-account view of everything, ignoring ./fly.toml")
@click.option("--apps-refresh", default=60, type=int,
              help="Multi-account view: org and app list refresh in seconds (default: 60)")
def main(mock: bool, refresh: int, accounts_path: Optional[Path], app: Optional[str],
         org: Optional[str], account: Optional[str], show_all: bool, apps_refresh: int):
    """Fly.io Terminal UI (ftui)"""
    if not use_fleet_view(accounts_path, bool(app or org or account), show_all):
        # Today's single-app view, unchanged.
        if mock:
            os.environ["FTUI_MOCK"] = "1"

        app = FTUI(refresh_interval=refresh)
        app.run()
        return

    from ftui.fleet_screen import build_fleet_screen

    accounts = None
    if not mock:
        try:
            accounts = load_accounts(accounts_path)
        except ConfigError as e:
            click.echo(f"ftui: {e}", err=True)
            sys.exit(1)

    screen = build_fleet_screen(
        accounts=accounts,
        mock=mock,
        refresh_interval=refresh,
        apps_interval=apps_refresh,
        initial_filter=initial_filter(app, org, account, use_fly_toml=not show_all),
    )
    FTUI(refresh_interval=refresh, fleet_screen=screen).run()


if __name__ == "__main__":
    main()
