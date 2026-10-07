"""Multi-account view: machines across Fly accounts, orgs and apps.

Used when there is an accounts config, no local fly.toml, or one of the
multi-account flags. The single-app screens in `ftui.app` are unchanged.
"""

import asyncio
import random
import subprocess
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Tree

from ftui.actions import AccountFlyClient
from ftui.app import LogScreen
from ftui.client import FlyError
from ftui.config import Account
from ftui.filters import Filter, next_state_mode, parse_filter
from ftui.fleet import AppRef, FleetClient, Machine
from ftui.inspect_screen import InspectScreen
from ftui.inspector import AppChoice, Inspector
from ftui.mock import MockFleet, mock_accounts

STATE_STYLES = {
    "started": "bold green",
    "stopped": "bold red",
    "suspended": "bold blue",
    "starting": "bold yellow",
    "stopping": "bold yellow",
    "replacing": "bold yellow",
    "created": "bold yellow",
}


def state_dot(state: str) -> Text:
    """Return a colored dot based on machine state."""
    return Text("●", style=STATE_STYLES.get(state, "bold grey50"))


def state_summary(machines: List[Machine], max_dots: int = 8) -> Text:
    """`●●○`-style summary: one coloured dot per machine, or counts if many."""
    text = Text()
    if len(machines) <= max_dots:
        for m in machines:
            text.append_text(state_dot(m.state))
        return text
    counts = Counter(m.state for m in machines)
    for state, n in sorted(counts.items()):
        text.append("●", style=STATE_STYLES.get(state, "bold grey50"))
        text.append(f"{n} ")
    return text


def below_min(machines: List[Machine]) -> bool:
    """True if an app runs fewer started machines than its min_machines_running."""
    wanted = max((m.min_running for m in machines), default=0)
    started = sum(1 for m in machines if m.state == "started")
    return wanted > 0 and started < wanted


@dataclass(frozen=True)
class Scope:
    """A sidebar selection: everything, an account, an org or an app."""
    account: Optional[str] = None
    org: Optional[str] = None
    app: Optional[str] = None

    def contains(self, account: str, org: str, app: str) -> bool:
        return (
            (self.account is None or self.account == account)
            and (self.org is None or self.org == org)
            and (self.app is None or self.app == app)
        )

    def describe(self) -> str:
        return " / ".join(p for p in (self.account, self.org, self.app) if p) or "all"


class ConfirmDialog(ModalScreen[bool]):
    """Yes/no confirmation for anything that changes infrastructure."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "Cancel", show=False),
    ]

    def __init__(self, title: str, target: str):
        super().__init__()
        self.title_text = title
        self.target = target

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self.title_text, id="dialog-title")
            yield Label(self.target, id="confirm-target")
            with Horizontal(id="dialog-buttons"):
                yield Label("[b]y[/b] Confirm  [b]n[/b]/[b]Esc[/b] Cancel")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class FleetScaleDialog(ModalScreen[Optional[Tuple[str, str]]]):
    """Modal for scaling an app's machines. Returns (count, vm_size)."""

    def __init__(self, target: str):
        super().__init__()
        self.target = target

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("Scale Configuration", id="dialog-title")
            yield Label(self.target)
            yield Label("Machine Count:")
            yield Input(placeholder="e.g., 3", id="scale-count", type="integer")
            yield Label("VM Size:")
            yield Input(placeholder="e.g., shared-cpu-1x", id="scale-vm")
            with Horizontal(id="dialog-buttons"):
                yield Label("[b]Enter[/b] Apply  [b]Esc[/b] Cancel")

    @on(Input.Submitted)
    def handle_submit(self) -> None:
        count = self.query_one("#scale-count", Input).value.strip()
        vm_size = self.query_one("#scale-vm", Input).value.strip()
        self.dismiss((count, vm_size) if (count or vm_size) else None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class FleetLogScreen(LogScreen):
    """The log viewer, for a machine in a given account and app."""

    def __init__(self, machine: Machine, account: Account, actions: AccountFlyClient):
        super().__init__(machine.id)
        self.machine = machine
        self.account = account
        self.actions = actions

    async def stream_logs(self) -> None:
        log = self.query_one(RichLog)

        if self.actions.mock:
            while True:
                log.write(f"[{self.machine_id}] Simulated log: {random.random()}")
                await asyncio.sleep(1)
            return

        m = self.machine
        log.write(f"{m.account} / {m.org} / {m.app}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.actions.logs_command(m.app, m.id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self.account.flyctl_env(),
            )
        except (FlyError, OSError) as e:
            log.write(f"Could not start logs: {e}")
            return

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                log.write(Text.from_ansi(line.decode().strip()))
        except asyncio.CancelledError:
            # Task was cancelled (screen popped), clean up
            pass
        finally:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass


COLUMNS = [
    ("Account", "account"),
    ("Org", "org"),
    ("App", "app"),
    ("Machine", "machine"),
    ("State", "state"),
    ("Checks", "checks"),
    ("Region", "region"),
    ("Size", "size"),
    ("Image", "image"),
    ("Updated", "updated"),
]


class FleetScreen(Screen):
    """Dashboard of machines across accounts, orgs and apps."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("f", "cycle_state", "Started/Stopped"),
        Binding("l", "logs", "Logs"),
        Binding("s", "scale", "Scale"),
        Binding("h", "ssh", "SSH"),
        Binding("ctrl+s", "start", "Start"),
        Binding("ctrl+x", "stop", "Stop"),
        Binding("ctrl+r", "restart", "Restart"),
        Binding("c", "inspect", "Inspect"),
    ]

    def __init__(
        self,
        fleet: FleetClient,
        actions: AccountFlyClient,
        refresh_interval: int = 5,
        apps_interval: int = 60,
        initial_filter: str = "",
        inspector: Optional[Inspector] = None,
    ):
        super().__init__()
        self.fleet = fleet
        self.actions = actions
        # Read-only: config, env, secret names/digests. Allowed on read-only accounts.
        self.inspector = inspector or Inspector(mock=actions.mock)
        self.refresh_interval = refresh_interval
        # Orgs and apps change rarely: list them less often than machines
        self.apps_interval = max(apps_interval, refresh_interval)
        self.apps: List[AppRef] = []
        self.machines: List[Machine] = []
        self.account_errors: Dict[str, str] = {}
        self.app_errors: Dict[Tuple[str, str], str] = {}
        self.initial_filter = initial_filter
        self.scope = Scope()
        # The node the user last selected. The filter can hide it, which moves
        # `scope` to the first match; it comes back once it matches again.
        self.user_scope = Scope()
        self.machine_filter: Filter = parse_filter(initial_filter)
        self.state_mode = "all"
        self.shown: List[Machine] = []
        self._tree_nodes: Dict[Scope, object] = {}
        self._tree_shape: Optional[Tuple] = None
        # (apps, accounts) shown in the sidebar, for the header while filtering
        self._nav_counts: Tuple[int, int] = (0, 0)
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree("All", id="sidebar")
            with Vertical(id="main"):
                yield Input(
                    value=self.initial_filter,
                    placeholder="/ filter: text  app:  org:  acct:  state:  region:   (e.g. state:stopped region:fra)",
                    id="filter",
                )
                yield DataTable(cursor_type="row", id="machines-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for label, key in COLUMNS:
            table.add_column(label, key=key)
        tree = self.query_one(Tree)
        tree.root.data = Scope()
        tree.root.expand()
        tree.show_root = True

        self.update_header()
        self.query_one(Header).tall = False
        table.focus()
        # Load in the background so the dashboard shows immediately
        self.run_worker(self.startup(), group="startup")

    async def startup(self) -> None:
        await self.fleet.resolve_tokens()
        await self.refresh_apps()
        self.set_interval(float(self.refresh_interval), self.refresh_data)
        self.set_interval(float(self.apps_interval), self.refresh_apps)

    # -- header ---------------------------------------------------------------

    def update_header(self, is_refreshing: bool = False) -> None:
        """Update the window title with current metadata."""
        interval = self.refresh_interval
        n_apps = len(self.apps)
        n_acct = len(self.fleet.accounts)
        if self.filtering:
            apps = f"{self._nav_counts[0]}/{n_apps} apps, {self._nav_counts[1]}/{n_acct} accounts"
        else:
            apps = f"{n_apps} apps, {n_acct} accounts"
        status = " (refreshing...)" if is_refreshing else ""
        shown = f"{len(self.shown)}/{len(self.machines)} machines"
        mode = "" if self.state_mode == "all" else f" [{self.state_mode}]"
        self.app.title = f"FTUI - {shown}, {apps}{mode} (refresh: {interval}s){status}"
        self.app.sub_title = self.scope.describe()

    # -- data -----------------------------------------------------------------

    async def refresh_apps(self) -> None:
        """Re-list orgs and apps (slow cadence), then refresh machines."""
        apps, errors = await self.fleet.list_apps()
        self.apps = apps
        self.account_errors = errors
        for name, err in errors.items():
            self.app.notify(f"{name}: {err}", severity="error")
        await self.refresh_data()

    async def refresh_data(self) -> None:
        """Re-fetch machines for every known app (fast cadence)."""
        if self._refreshing:
            return
        self._refreshing = True
        self.update_header(is_refreshing=True)
        try:
            machines, errors = await self.fleet.list_machines(self.apps)
            self.machines = machines
            self.app_errors = errors
            self.render_tree()
            self.render_table()
        finally:
            self._refreshing = False
            self.update_header(is_refreshing=False)

    # -- sidebar --------------------------------------------------------------

    def _group(self) -> Dict[Tuple[str, str, str], List[Machine]]:
        groups: Dict[Tuple[str, str, str], List[Machine]] = {}
        for app in self.apps:
            groups[(app.account, app.org, app.name)] = []
        for m in self.machines:
            groups.setdefault((m.account, m.org, m.app), []).append(m)
        return groups

    def _label(self, name: str, machines: List[Machine], error: str = "", warn: bool = False,
               read_only: bool = False) -> Text:
        text = Text(name, style="bold" if not machines and not error else "")
        if read_only:
            text.append(" ro", style="dim italic")
        if error:
            text.append(" ✖ ", style="bold red")
            text.append(error[:40], style="red")
            return text
        if self.filtering:
            # matched/total; the dots show the matching machines only
            matched = [m for m in machines if self.matches(m)]
            text.append(f" {len(matched)}/{len(machines)} ", style="dim")
            text.append_text(state_summary(matched))
        else:
            text.append(f" {len(machines)} ", style="dim")
            text.append_text(state_summary(machines))
        if warn:
            text.append(" ⚠", style="bold yellow")
        return text

    def _visible_nav(
        self, groups: Dict[Tuple[str, str, str], List[Machine]]
    ) -> Tuple[set, set]:
        """(app keys, account names) the sidebar shows under the filter and `f`.

        An app is shown if any of its machines match, or if the filter matches
        the app itself by name (so apps with 0 machines, or that failed to
        load, still show for `app:`, `org:`, `acct:` or free text). An account
        with no apps at all is shown if the filter matches its name.
        """
        accounts = self.fleet.accounts
        if not self.filtering:
            return set(groups), set(accounts) | {a for a, _, _ in groups}
        f = self.machine_filter
        by_name = self.state_mode == "all"
        apps = {
            key for key, ms in groups.items()
            if any(self.matches(m) for m in ms) or (by_name and f.matches_scope(*key))
        }
        with_apps = {a for a, _, _ in groups}
        names = {a for a, _, _ in apps} | {
            a for a in accounts if a not in with_apps and by_name and f.matches_scope(a)
        }
        return apps, names

    def render_tree(self) -> None:
        tree = self.query_one(Tree)
        groups = self._group()
        accounts = self.fleet.accounts
        errors = self.account_errors
        visible_apps, visible_accounts = self._visible_nav(groups)
        self._nav_counts = (len(visible_apps), len(visible_accounts))

        # account -> org -> [app], only what the filter leaves
        shape: Dict[str, Dict[str, List[str]]] = {
            name: {} for name in accounts if name in visible_accounts
        }
        for (account, org, app) in sorted(groups):
            if (account, org, app) in visible_apps:
                shape.setdefault(account, {}).setdefault(org, []).append(app)
        shape_key = tuple((a, tuple((o, tuple(apps)) for o, apps in orgs.items()))
                          for a, orgs in shape.items())

        if shape_key != self._tree_shape:
            self._tree_shape = shape_key
            tree.clear()
            tree.root.data = Scope()
            self._tree_nodes = {Scope(): tree.root}
            for account, orgs in shape.items():
                a_node = tree.root.add(account, data=Scope(account), expand=True)
                self._tree_nodes[Scope(account)] = a_node
                for org, apps in orgs.items():
                    o_node = a_node.add(org, data=Scope(account, org), expand=True)
                    self._tree_nodes[Scope(account, org)] = o_node
                    for app in apps:
                        s = Scope(account, org, app)
                        self._tree_nodes[s] = o_node.add_leaf(app, data=s)
            tree.root.expand()
            self._settle_scope()
            # node lines exist once the tree has laid out the new nodes
            self.call_after_refresh(
                lambda: tree.move_cursor(self._tree_nodes.get(self.scope, tree.root))
            )

        def under(scope: Scope) -> List[Machine]:
            return [m for key, ms in groups.items() if scope.contains(*key) for m in ms]

        all_machines = [m for ms in groups.values() for m in ms]
        tree.root.set_label(self._label("All", all_machines))
        for scope, node in self._tree_nodes.items():
            if scope.app:
                ms = groups.get((scope.account, scope.org, scope.app), [])
                err = self.app_errors.get((scope.account, scope.app), "")
                node.set_label(self._label(scope.app, ms, error=err, warn=below_min(ms)))
            elif scope.org:
                node.set_label(self._label(scope.org, under(scope)))
            elif scope.account:
                account = accounts.get(scope.account)
                node.set_label(self._label(
                    scope.account, under(scope), error=errors.get(scope.account, ""),
                    read_only=bool(account and account.read_only),
                ))

    def _settle_scope(self) -> None:
        """Keep the selected node if it still shows, else pick the first match.

        The first match is the first shown node at the same level (app, org or
        account), preferring one with matching machines, falling back to All.
        The user's own choice is remembered and comes back when it matches
        again, e.g. once the filter is cleared.
        """
        if self.user_scope in self._tree_nodes:
            self.scope = self.user_scope
            return

        def level(s: Scope) -> int:
            return 3 if s.app else 2 if s.org else 1 if s.account else 0

        def has_matches(s: Scope) -> bool:
            return any(s.contains(m.account, m.org, m.app) and self.matches(m)
                       for m in self.machines)

        same = [s for s in self._tree_nodes if level(s) == level(self.user_scope)]
        same.sort(key=lambda s: not has_matches(s))  # stable: tree order otherwise
        self.scope = same[0] if same else Scope()

    @on(Tree.NodeSelected, "#sidebar")
    def on_scope_selected(self, event: Tree.NodeSelected) -> None:
        if isinstance(event.node.data, Scope):
            self.scope = self.user_scope = event.node.data
            self.render_table()
            self.update_header()

    # -- table ----------------------------------------------------------------

    @property
    def filtering(self) -> bool:
        """True while the filter bar or `f` hides anything."""
        return not self.machine_filter.empty or self.state_mode != "all"

    def matches(self, m: Machine) -> bool:
        """The filter bar and `f`, regardless of the sidebar selection."""
        if self.state_mode != "all" and m.state != self.state_mode:
            return False
        return self.machine_filter.matches(m)

    def is_visible(self, m: Machine) -> bool:
        return self.scope.contains(m.account, m.org, m.app) and self.matches(m)

    @staticmethod
    def row_for(m: Machine) -> list:
        machine = Text(m.id)
        machine.append(f" {m.name}", style="dim")
        state = state_dot(m.state)
        state.append(f" {m.state}")
        return [
            m.account, m.org, m.app, machine, state, m.checks or "-",
            m.region, m.size, m.image, m.updated_at,
        ]

    def render_table(self) -> None:
        table = self.query_one(DataTable)

        # Save current selection by key
        selected_key = self.selected_key()

        self.shown = [m for m in self.machines if self.is_visible(m)]
        current_keys = [row.key.value for row in table.ordered_rows]
        new_keys = [m.key for m in self.shown]

        if current_keys == new_keys:
            # Same rows in the same order: update values in place to avoid flicker
            for m in self.shown:
                for (_, col), value in zip(COLUMNS, self.row_for(m)):
                    table.update_cell(m.key, col, value)
        else:
            # Full refresh only if structure changed
            table.clear()
            new_cursor_row = 0
            for i, m in enumerate(self.shown):
                table.add_row(*self.row_for(m), key=m.key)
                if m.key == selected_key:
                    new_cursor_row = i
            if self.shown:
                table.move_cursor(row=new_cursor_row)
        self.update_header()

    def selected_key(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if not table.row_count or table.cursor_row is None:
            return None
        try:
            return table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        except Exception:
            return None

    def selected_machine(self) -> Optional[Machine]:
        key = self.selected_key()
        machine = next((m for m in self.shown if m.key == key), None)
        if machine is None:
            self.app.notify("No machine selected")
        return machine

    # -- filter ---------------------------------------------------------------

    @on(Input.Changed, "#filter")
    def on_filter_changed(self, event: Input.Changed) -> None:
        self.machine_filter = parse_filter(event.value)
        self.render_tree()
        self.render_table()

    @on(Input.Submitted, "#filter")
    def on_filter_submitted(self) -> None:
        self.query_one(DataTable).focus()

    def on_key(self, event) -> None:
        if event.key == "escape" and self.focused is self.query_one("#filter", Input):
            self.query_one(DataTable).focus()
            event.stop()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_cycle_state(self) -> None:
        self.state_mode = next_state_mode(self.state_mode)
        self.app.notify(f"Showing {self.state_mode} machines")
        self.render_tree()
        self.render_table()

    # -- actions --------------------------------------------------------------

    def _target(self, m: Machine, include_machine: bool = True) -> str:
        parts = [f"account: {m.account}", f"org:     {m.org}", f"app:     {m.app}"]
        if include_machine:
            parts.append(f"machine: {m.id} ({m.name})")
        return "\n".join(parts)

    def _writable(self, m: Machine) -> Optional[Account]:
        account = self.fleet.accounts.get(m.account)
        if account is None:
            self.app.notify(f"Unknown account {m.account}", severity="error")
            return None
        if account.read_only:
            self.app.notify(f"Account {m.account} is read-only", severity="warning")
            return None
        return account

    def action_logs(self) -> None:
        if m := self.selected_machine():
            self.app.push_screen(FleetLogScreen(m, self.fleet.accounts[m.account], self.actions))

    def action_inspect(self) -> None:
        """Read-only panel for the selected machine's app. Works on read-only accounts."""
        m = self.selected_machine()
        if m is None:
            return
        accounts = self.fleet.accounts
        peers = [AppChoice(a.name, accounts[a.account]) for a in self.apps if a.account in accounts]

        def known_env(choice: AppChoice, group: str) -> Optional[Tuple[Dict[str, str], str]]:
            owner = choice.account.name if choice.account else None
            ms = [x for x in self.machines if x.app == choice.app and x.account == owner]
            if not ms:
                return None
            chosen = next((x for x in ms if group and x.process_group == group), ms[0])
            return dict(chosen.env), chosen.id

        self.app.push_screen(InspectScreen(
            AppChoice(m.app, accounts.get(m.account)), m.id, dict(m.env), self.inspector,
            process_group=m.process_group, peers=peers, known_env=known_env,
            context=f"{m.account} / {m.org}",
        ))

    def action_ssh(self) -> None:
        if (m := self.selected_machine()) and (account := self._writable(m)):
            if self.actions.mock:
                self.app.notify("SSH is not available in mock mode")
                return
            with self.app.suspend():
                subprocess.run(self.actions.ssh_command(m.app, m.id), env=account.flyctl_env())

    def _confirm_machine_op(self, verb: str, method: str) -> None:
        m = self.selected_machine()
        if not m or not (account := self._writable(m)):
            return

        def done(ok: Optional[bool]) -> None:
            if ok:
                op = getattr(self.actions, method)(account, m.app, m.id)
                self.run_worker(self.run_op(op, f"{verb} {m.app}/{m.id}"))

        self.app.push_screen(ConfirmDialog(f"{verb} machine?", self._target(m)), done)

    def action_start(self) -> None:
        self._confirm_machine_op("Start", "start_machine")

    def action_stop(self) -> None:
        self._confirm_machine_op("Stop", "stop_machine")

    def action_restart(self) -> None:
        self._confirm_machine_op("Restart", "restart_machine")

    def action_scale(self) -> None:
        m = self.selected_machine()
        if not m or not (account := self._writable(m)):
            return
        target = self._target(m, include_machine=False)

        def confirmed(values: Tuple[str, str], ok: Optional[bool]) -> None:
            if not ok:
                return
            count, vm_size = values

            async def op() -> None:
                if count:
                    await self.actions.scale_count(account, m.app, int(count))
                if vm_size:
                    await self.actions.scale_vm(account, m.app, vm_size)

            self.run_worker(self.run_op(op(), f"Scaling {m.app}"))

        def entered(values: Optional[Tuple[str, str]]) -> None:
            if not values:
                return
            count, vm_size = values
            what = ", ".join(filter(None, [
                f"count -> {count}" if count else "", f"vm -> {vm_size}" if vm_size else ""
            ]))
            self.app.push_screen(
                ConfirmDialog(f"Scale app ({what})?", target),
                lambda ok: confirmed(values, ok),
            )

        self.app.push_screen(FleetScaleDialog(target), entered)

    async def run_op(self, coro, message: str) -> None:
        self.app.notify(message)
        try:
            await coro
            await self.refresh_data()
        except FlyError as e:
            self.app.notify(str(e), severity="error")

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_data())

    async def on_unmount(self) -> None:
        await self.fleet.aclose()


def build_fleet_screen(
    accounts: Optional[Iterable[Account]] = None,
    mock: bool = False,
    refresh_interval: int = 5,
    apps_interval: int = 60,
    initial_filter: str = "",
) -> FleetScreen:
    """A FleetScreen over real accounts, or over the simulated fleet with mock=True."""
    if mock:
        mock_fleet = MockFleet()
        fleet = FleetClient(mock_accounts(), transport=mock_fleet.transport())
        actions = AccountFlyClient(mock_fleet=mock_fleet)
    else:
        fleet = FleetClient(accounts or [Account(name="local", token_source="fly")])
        actions = AccountFlyClient()
    return FleetScreen(fleet, actions, refresh_interval, apps_interval, initial_filter)
