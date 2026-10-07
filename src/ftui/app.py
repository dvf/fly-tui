import asyncio
import os
import random
import subprocess
from typing import List, Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

from ftui.client import FlyClient, FlyError, Machine


class ScaleDialog(ModalScreen):
    """Modal for scaling app machines."""

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("Scale Configuration", id="dialog-title")
            yield Label("Machine Count:")
            yield Input(placeholder="e.g., 3", id="scale-count", type="integer")
            yield Label("VM Size:")
            yield Input(placeholder="e.g., shared-cpu-1x", id="scale-vm")
            with Horizontal(id="dialog-buttons"):
                yield Label("[b]Enter[/b] Apply  [b]Esc[/b] Cancel")

    @on(Input.Submitted)
    async def handle_submit(self) -> None:
        count = self.query_one("#scale-count", Input).value
        vm_size = self.query_one("#scale-vm", Input).value
        
        client = self.app.client
        try:
            if count:
                await client.scale_count(int(count))
                self.app.notify(f"Scaling count to {count}")
            if vm_size:
                await client.scale_vm(vm_size)
                self.app.notify(f"Scaling VM to {vm_size}")
            self.dismiss()
        except FlyError as e:
            self.app.notify(str(e), severity="error")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()


class LogScreen(Screen):
    """Real-time log viewer."""

    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("c", "clear", "Clear"),
        Binding("ctrl+c", "copy_logs", "Copy All"),
    ]

    def __init__(self, machine_id: str):
        super().__init__()
        self.machine_id = machine_id
        self._task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Header()
        log = RichLog(id="log-viewer", highlight=True, markup=True)
        log.can_focus = True
        yield log
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.focus()
        log.write(f"Connecting to logs for {self.machine_id}...")
        self._task = asyncio.create_task(self.stream_logs())

    async def stream_logs(self) -> None:
        log = self.query_one(RichLog)
        
        if self.app.client.mock:
            while True:
                log.write(f"[{self.machine_id}] Simulated log: {random.random()}")
                await asyncio.sleep(1)
            return

        proc = await asyncio.create_subprocess_exec(
            "fly", "logs", "--instance", self.machine_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        try:
            while True:
                line = await proc.stdout.readline()
                if not line: break
                log.write(Text.from_ansi(line.decode().strip()))
        except asyncio.CancelledError:
            # Task was cancelled (screen popped), clean up
            pass
        finally:
            try: 
                proc.kill()
                await proc.wait()
            except: 
                pass

    def copy_to_clipboard(self, text: str) -> None:
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(text.encode())
        except Exception:
            self.app.notify("Clipboard copy failed (pbcopy not found?)", severity="error")

    def action_copy_logs(self) -> None:
        log = self.query_one(RichLog)
        all_text = "\n".join(line.text for line in log.lines)
        self.copy_to_clipboard(all_text)
        self.app.notify("All logs copied to clipboard")

    def action_clear(self) -> None:
        self.query_one(RichLog).clear()

    def action_back(self) -> None:
        if self._task: self._task.cancel()
        self.app.pop_screen()


class MachineListScreen(Screen):
    """Main dashboard showing machine status and controls."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("l", "logs", "Logs"),
        Binding("s", "scale", "Scale"),
        Binding("h", "ssh", "SSH"),
        Binding("ctrl+s", "start", "Start"),
        Binding("ctrl+x", "stop", "Stop"),
        Binding("ctrl+r", "restart", "Restart"),
        Binding("c", "inspect", "Inspect"),
    ]

    # Last machines listed; the inspect panel reads a machine's env from here.
    machines: List[Machine] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row", id="machines-table", fixed_columns=1)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(
            "●", "ID", "Name", "Process", "State", "Region", 
            "CPU", "RAM", "HTTP", "Image", "Created"
        )
        
        self.update_header()
        self.query_one(Header).tall = False
        
        await self.refresh_data()
        self.set_interval(float(self.app.client.refresh_interval), self.refresh_data)

    def update_header(self, is_refreshing: bool = False) -> None:
        """Update the window title with current metadata."""
        interval = self.app.client.refresh_interval
        name = getattr(self.app, "app_name", "Loading...")
        status = " (refreshing...)" if is_refreshing else ""
        self.app.title = f"FTUI - {name} (refresh: {interval}s){status}"

    def get_state_dot(self, state: str) -> Text:
        """Return a colored dot based on machine state."""
        if state == "started":
            return Text("●", style="bold green")
        elif state == "stopped":
            return Text("●", style="bold red")
        elif state == "starting" or state == "stopping":
            return Text("●", style="bold yellow")
        return Text("●", style="bold grey50")

    async def refresh_data(self) -> None:
        table = self.query_one(DataTable)
        self.update_header(is_refreshing=True)
        
        # Save current selection by ID
        selected_id = None
        if table.cursor_row is not None:
            try:
                # selected_id is in the second column (index 1) now
                selected_id = table.get_row_at(table.cursor_row)[1]
            except Exception:
                pass

        try:
            machines = await self.app.client.list_machines()
            self.machines = machines
            
            # If the IDs and count match exactly, just update values to avoid flicker
            current_ids = [table.get_row_at(i)[1] for i in range(table.row_count)]
            new_ids = [m.id for m in machines]
            
            if current_ids == new_ids:
                for i, m in enumerate(machines):
                    # Update each cell in the row
                    row_data = [
                        self.get_state_dot(m.state), m.id, m.name, m.process_group, 
                        m.state, m.region, m.cpu, m.ram, "Yes" if m.has_http else "No",
                        m.image, m.created_at
                    ]
                    for col_idx, value in enumerate(row_data):
                        table.update_cell_at((i, col_idx), value)
            else:
                # Full refresh only if structure changed
                table.clear()
                new_cursor_row = 0
                for i, m in enumerate(machines):
                    table.add_row(
                        self.get_state_dot(m.state), m.id, m.name, m.process_group, 
                        m.state, m.region, m.cpu, m.ram, "Yes" if m.has_http else "No",
                        m.image, m.created_at
                    )
                    if m.id == selected_id:
                        new_cursor_row = i
                
                if machines:
                    table.move_cursor(row=new_cursor_row)
                    
        except FlyError as e:
            self.app.notify(f"Refresh failed: {e}", severity="error")
        finally:
            self.update_header(is_refreshing=False)

    def get_selected_id(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.app.notify("No machine selected")
            return None
        return table.get_row_at(table.cursor_row)[1]

    def action_logs(self) -> None:
        if mid := self.get_selected_id():
            self.app.push_screen(LogScreen(mid))

    def action_scale(self) -> None:
        self.app.push_screen(ScaleDialog())

    def action_inspect(self) -> None:
        """Read-only panel: config, env, secret names/digests, diff vs a sibling app."""
        from ftui.inspect_screen import InspectScreen
        from ftui.inspector import AppChoice, Inspector

        if not self.query_one(DataTable).row_count:
            self.app.notify("No machine selected")
            return
        mid = self.get_selected_id()
        machine = next((m for m in self.machines if m.id == mid), None)
        if machine is None:
            return
        name = getattr(self.app, "app_name", "")
        if name in ("Loading...", "Unknown App"):
            name = ""  # flyctl falls back to ./fly.toml
        self.app.push_screen(InspectScreen(
            AppChoice(name), machine.id, dict(machine.env),
            Inspector(mock=self.app.client.mock), process_group=machine.process_group,
        ))

    def action_ssh(self) -> None:
        if mid := self.get_selected_id():
            with self.app.suspend():
                subprocess.run(["fly", "ssh", "console", "-s", "-m", mid])

    async def action_start(self) -> None:
        if mid := self.get_selected_id():
            await self.run_op(self.app.client.start_machine(mid), f"Starting {mid}")

    async def action_stop(self) -> None:
        if mid := self.get_selected_id():
            await self.run_op(self.app.client.stop_machine(mid), f"Stopping {mid}")

    async def action_restart(self) -> None:
        if mid := self.get_selected_id():
            await self.run_op(self.app.client.restart_machine(mid), f"Restarting {mid}")

    async def run_op(self, coro, message: str) -> None:
        self.app.notify(message)
        try:
            await coro
            await self.refresh_data()
        except FlyError as e:
            self.app.notify(str(e), severity="error")

    def action_refresh(self) -> None:
        asyncio.create_task(self.refresh_data())


class FTUI(App):
    """Main application entry point."""

    CSS_PATH = "styles.tcss"

    def __init__(self, refresh_interval: int = 5, fleet_screen: Optional[Screen] = None):
        super().__init__()
        self.client = FlyClient(refresh_interval=refresh_interval)
        self.app_name: str = "Loading..."
        # Multi-account view (ftui.fleet_screen); None keeps the single-app view.
        self.fleet_screen = fleet_screen

    def on_mount(self) -> None:
        if self.fleet_screen is not None:
            self.push_screen(self.fleet_screen)
            return
        # Show the dashboard immediately
        self.push_screen(MachineListScreen())
        self.run_worker(self.initialize_app_name())

    async def initialize_app_name(self) -> None:
        """Fetch initial metadata in background and update header."""
        try:
            self.app_name = await self.client.get_app_name()
            # If the current screen has an update_header method, call it
            if hasattr(self.screen, "update_header"):
                self.screen.update_header()
        except Exception:
            self.app_name = "Unknown App"
            if hasattr(self.screen, "update_header"):
                self.screen.update_header()


if __name__ == "__main__":
    FTUI().run()
