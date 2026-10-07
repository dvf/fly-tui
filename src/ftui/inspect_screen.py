"""The read-only inspect panel (`c` on a machine): Config, Env, Secrets, Diff.

Opened from both the multi-account view and the single-app view. It only
reads, so it works for read-only accounts. Secret values are never fetched
or shown: the Secrets and Diff tabs work from names and digests alone.
"""

from typing import Callable, Dict, List, Optional, Tuple

from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable, Footer, Input, Label, OptionList, Static, TabbedContent, TabPane,
)
from textual.widgets.option_list import Option

from ftui.client import FlyError
from ftui.inspector import (
    AppChoice, Inspector, Secret, diff_env, diff_secrets, sibling_app,
)

# Given a diff target and a process group, env and machine id already known to
# the caller (e.g. the fleet view's machines), or None to fetch it.
KnownEnv = Callable[[AppChoice, str], Optional[Tuple[Dict[str, str], str]]]

SAME_VALUE = "same value on both"


def error_text(what: str, err: Exception) -> Text:
    text = Text("✖ ", style="bold red")
    text.append(f"{what}: ", style="bold")
    text.append(str(err) or type(err).__name__, style="red")
    return text


class AppPicker(ModalScreen[Optional[int]]):
    """Pick the app to diff against. Returns an index into `choices`."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, choices: List[AppChoice]):
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Container(id="picker"):
            yield Label("Diff against which app?", id="dialog-title")
            yield Input(placeholder="type to filter", id="picker-filter")
            yield OptionList(id="picker-list")
            yield Label("[b]Enter[/b] Pick  [b]Esc[/b] Cancel")

    def on_mount(self) -> None:
        self.fill("")
        self.query_one(Input).focus()

    def fill(self, text: str) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        text = text.lower()
        options.add_options([
            Option(c.label, id=str(i)) for i, c in enumerate(self.choices)
            if text in c.label.lower()
        ])
        if options.option_count:
            options.highlighted = 0

    @on(Input.Changed, "#picker-filter")
    def on_filter(self, event: Input.Changed) -> None:
        self.fill(event.value)

    @on(Input.Submitted, "#picker-filter")
    def on_filter_submitted(self) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None and options.option_count:
            self.dismiss(int(options.get_option_at_index(options.highlighted).id))

    def on_key(self, event) -> None:
        # arrows move through the list while typing in the filter
        if event.key in ("up", "down") and self.focused is self.query_one(Input):
            options = self.query_one(OptionList)
            if options.option_count:
                current = options.highlighted or 0
                step = 1 if event.key == "down" else -1
                options.highlighted = max(0, min(options.option_count - 1, current + step))
            event.stop()

    @on(OptionList.OptionSelected)
    def on_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class InspectScreen(ModalScreen):
    """Deployed config, machine env, secret metadata and a diff for one app."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("a", "pick_diff", "Diff against…"),
        Binding("1", "tab('tab-config')", "Config", show=False),
        Binding("2", "tab('tab-env')", "Env", show=False),
        Binding("3", "tab('tab-secrets')", "Secrets", show=False),
        Binding("4", "tab('tab-diff')", "Diff", show=False),
    ]

    def __init__(
        self,
        target: AppChoice,
        machine_id: str,
        env: Dict[str, str],
        inspector: Inspector,
        process_group: str = "",
        peers: Optional[List[AppChoice]] = None,
        known_env: Optional[KnownEnv] = None,
        context: str = "",
    ):
        super().__init__()
        self.target = target
        self.machine_id = machine_id
        self.env = dict(env)
        self.inspector = inspector
        self.process_group = process_group if process_group not in ("", "-") else ""
        # None: list them with the inspector (single-app view)
        self.peers = peers
        self.known_env = known_env
        self.context = context
        self.secrets: Optional[List[Secret]] = None
        self.secrets_error: Optional[Exception] = None
        self.diff_target: Optional[AppChoice] = None
        self._diff_generation = 0

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="inspect"):
            title = Text(f"Inspect {self.target.label}", style="bold")
            title.append(f"   machine {self.machine_id}", style="dim")
            if self.context:
                title.append(f"   {self.context}", style="dim")
            title.append("   (read-only view)", style="dim italic")
            yield Static(title, id="inspect-title")
            with TabbedContent(initial="tab-config"):
                with TabPane("Config", id="tab-config"):
                    with VerticalScroll():
                        yield Static("Loading config…", id="config-body")
                with TabPane("Env", id="tab-env"):
                    yield Static("", id="env-msg")
                    yield DataTable(cursor_type="row", id="env-table")
                with TabPane("Secrets", id="tab-secrets"):
                    yield Static("Loading secrets…", id="secrets-msg")
                    yield DataTable(cursor_type="row", id="secrets-table")
                with TabPane("Diff", id="tab-diff"):
                    yield Static("Finding an app to compare…", id="diff-msg")
                    yield DataTable(cursor_type="row", id="diff-table")
        yield Footer()

    def on_mount(self) -> None:
        env_table = self.query_one("#env-table", DataTable)
        env_table.add_column("Key", key="key")
        env_table.add_column("Value", key="value")
        secrets = self.query_one("#secrets-table", DataTable)
        # No value column: secret values are never fetched.
        for label in ("Name", "Digest", "Status", "Created"):
            secrets.add_column(label, key=label.lower())
        diff = self.query_one("#diff-table", DataTable)
        for label in ("Kind", "Name", "Difference", "This app", "Other app"):
            diff.add_column(label)

        self.render_env()
        self.run_worker(self.load_config(), group="inspect")
        self.run_worker(self.load_secrets_then_diff(), group="inspect")

    # -- tabs -------------------------------------------------------------------

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_close(self) -> None:
        self.workers.cancel_group(self, "inspect")
        self.dismiss(None)

    async def load_config(self) -> None:
        body = self.query_one("#config-body", Static)
        try:
            text = await self.inspector.config_toml(self.target)
        except (FlyError, OSError, ValueError) as e:
            body.update(error_text("config", e))
            return
        self.config_text = text
        body.update(Syntax(text, "toml", theme="ansi_dark", word_wrap=True))

    def render_env(self) -> None:
        table = self.query_one("#env-table", DataTable)
        msg = self.query_one("#env-msg", Static)
        table.clear()
        for key in sorted(self.env):
            table.add_row(key, self.env[key], key=key)
        group = f", process group {self.process_group}" if self.process_group else ""
        msg.update(Text(
            f"{len(self.env)} env vars on machine {self.machine_id}{group} (config.env)",
            style="dim",
        ) if self.env else Text(f"No env vars on machine {self.machine_id}", style="dim"))

    async def load_secrets_then_diff(self) -> None:
        msg = self.query_one("#secrets-msg", Static)
        table = self.query_one("#secrets-table", DataTable)
        try:
            self.secrets = await self.inspector.secrets(self.target)
        except (FlyError, OSError, ValueError) as e:
            self.secrets_error = e
            msg.update(error_text("secrets", e))
        else:
            table.clear()
            for s in self.secrets:
                table.add_row(s.name, s.digest or "-", self.status_text(s.status),
                              s.created_at or "-", key=s.name)
            msg.update(Text(
                f"{len(self.secrets)} secrets: names, digests and status only "
                "(values are never fetched)", style="dim",
            ))
        await self.choose_default_diff()

    @staticmethod
    def status_text(status: str) -> Text:
        style = {"deployed": "green", "staged": "yellow", "partial": "yellow"}.get(
            status.lower(), "dim")
        return Text(status or "-", style=style)

    # -- diff -------------------------------------------------------------------

    async def load_peers(self) -> List[AppChoice]:
        if self.peers is None:
            try:
                names = await self.inspector.list_apps(self.target.account)
            except (FlyError, OSError, ValueError) as e:
                self.query_one("#diff-msg", Static).update(error_text("app list", e))
                names = []
            self.peers = [AppChoice(n, self.target.account) for n in names]
        return [p for p in self.peers if not p.same_as(self.target)]

    async def choose_default_diff(self) -> None:
        peers = await self.load_peers()
        msg = self.query_one("#diff-msg", Static)
        name = sibling_app(self.target.app, [p.app for p in peers])
        if name is None:
            if peers:
                msg.update(Text("No -staging sibling found. Press a to pick an app to diff against.",
                                style="dim"))
            return
        same_account = [p for p in peers if p.app == name and self.same_account(p)]
        choice = (same_account or [p for p in peers if p.app == name])[0]
        await self.run_diff(choice)

    def same_account(self, other: AppChoice) -> bool:
        a, b = self.target.account, other.account
        return (a.name if a else None) == (b.name if b else None)

    def action_pick_diff(self) -> None:
        self.run_worker(self._pick_diff(), group="inspect")

    async def _pick_diff(self) -> None:
        peers = await self.load_peers()
        if not peers:
            self.app.notify("No other apps to diff against")
            return

        def picked(index: Optional[int]) -> None:
            if index is not None:
                self.action_tab("tab-diff")
                self.run_worker(self.run_diff(peers[index]), group="inspect")

        self.app.push_screen(AppPicker(peers), picked)

    async def other_env(self, other: AppChoice) -> Tuple[Dict[str, str], str]:
        if self.known_env and (known := self.known_env(other, self.process_group)):
            return known
        return await self.inspector.machine_env(other, self.process_group)

    async def run_diff(self, other: AppChoice) -> None:
        self._diff_generation += 1
        generation = self._diff_generation
        self.diff_target = other
        msg = self.query_one("#diff-msg", Static)
        table = self.query_one("#diff-table", DataTable)
        msg.update(Text(f"Comparing with {other.label}…", style="dim"))

        env_error = secrets_error = None
        try:
            env_b, machine_b = await self.other_env(other)
        except (FlyError, OSError, ValueError) as e:
            env_b, machine_b, env_error = {}, "", e
        try:
            secrets_b = await self.inspector.secrets(other)
        except (FlyError, OSError, ValueError) as e:
            secrets_b, secrets_error = [], e
        if self.secrets_error:
            secrets_error = self.secrets_error
        if generation != self._diff_generation:
            return  # a newer pick replaced this one

        table.clear()
        this, that = self.target.app or "this app", other.app

        sd = None
        if secrets_error is None:
            sd = diff_secrets(self.secrets or [], secrets_b)
            for name in sd.same_digest:
                table.add_row("secret", name, Text(SAME_VALUE, style="bold yellow"),
                              "same digest", "same digest")
            for name in sd.only_a:
                table.add_row("secret", name, f"only in {this}", "present", "-")
            for name in sd.only_b:
                table.add_row("secret", name, f"only in {that}", "-", "present")
            for name in sd.different:
                table.add_row("secret", name, Text("different value", style="dim"),
                              "", "")

        ed = None
        if env_error is None:
            ed = diff_env(self.env, env_b)
            for key, a, b in ed.differ:
                table.add_row("env", key, "differs", a, b)
            for key in ed.only_a:
                table.add_row("env", key, f"only in {this}", self.env[key], "-")
            for key in ed.only_b:
                table.add_row("env", key, f"only in {that}", "-", env_b[key])

        summary = Text()
        summary.append(f"{this} ↔ {other.label}", style="bold")
        if machine_b:
            summary.append(f"   env from machine {self.machine_id} vs {machine_b}", style="dim")
        summary.append("   a: pick another app\n", style="dim")
        if sd is not None:
            summary.append("secrets: ")
            summary.append(f"{len(sd.same_digest)} {SAME_VALUE}",
                           style="bold yellow" if sd.same_digest else "")
            summary.append(f", {len(sd.only_a)} only in {this}, {len(sd.only_b)} only in {that}, "
                           f"{len(sd.different)} different\n")
        else:
            summary.append_text(error_text("secrets", secrets_error))
            summary.append("\n")
        if ed is not None:
            summary.append(f"env: {len(ed.differ)} differ, {len(ed.only_a)} only in {this}, "
                           f"{len(ed.only_b)} only in {that}, {len(ed.same)} same")
        else:
            summary.append_text(error_text("env", env_error))
        msg.update(summary)
