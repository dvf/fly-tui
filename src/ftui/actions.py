"""flyctl wrapper for actions in the multi-account view (start, stop, restart,
scale, logs, ssh).

Every call names its app with `-a` and runs with the owning account's token
in the subprocess environment (`FLY_API_TOKEN`), so actions always land in
the right account no matter which directory ftui was started from.
"""

import asyncio
import shutil
from typing import List

from ftui.client import FlyError
from ftui.config import Account


class AccountFlyClient:
    """Runs flyctl commands as a given account, against a named app.

    The single-app `ftui.client.FlyClient` is unchanged; this is its
    multi-account counterpart.
    """

    def __init__(self, mock_fleet=None, timeout: float = 60.0):
        self.mock_fleet = mock_fleet
        self.timeout = timeout
        self._bin = shutil.which("fly") or shutil.which("flyctl")

    @property
    def mock(self) -> bool:
        return self.mock_fleet is not None

    @property
    def bin(self) -> str:
        if not self._bin:
            raise FlyError("flyctl not found in PATH")
        return self._bin

    def command(self, *args: str) -> List[str]:
        return [self.bin, *args]

    async def _exec(self, account: Account, *args: str) -> str:
        """Execute a flyctl command as `account` and return stdout."""
        if account.read_only:
            raise FlyError(f"account {account.name} is read-only")
        if self.mock:
            return await self._mock_exec(*args)

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=account.flyctl_env(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            if proc.returncode != 0:
                raise FlyError(stderr.decode().strip() or f"Command failed with code {proc.returncode}")
            return stdout.decode()
        except asyncio.TimeoutError:
            if proc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise FlyError(f"Command timed out: fly {' '.join(args)}")
        except OSError as e:
            raise FlyError(str(e))

    async def restart_machine(self, account: Account, app: str, machine_id: str) -> None:
        await self._exec(account, "machine", "restart", machine_id, "-a", app, "--yes")

    async def stop_machine(self, account: Account, app: str, machine_id: str) -> None:
        await self._exec(account, "machine", "stop", machine_id, "-a", app)

    async def start_machine(self, account: Account, app: str, machine_id: str) -> None:
        await self._exec(account, "machine", "start", machine_id, "-a", app)

    async def scale_count(self, account: Account, app: str, count: int) -> None:
        await self._exec(account, "scale", "count", str(count), "-a", app, "--yes")

    async def scale_vm(self, account: Account, app: str, size: str) -> None:
        await self._exec(account, "scale", "vm", size, "-a", app, "--yes")

    def logs_command(self, app: str, machine_id: str) -> List[str]:
        return self.command("logs", "-a", app, "--machine", machine_id)

    def ssh_command(self, app: str, machine_id: str) -> List[str]:
        return self.command("ssh", "console", "-a", app, "--machine", machine_id)

    async def _mock_exec(self, *args: str) -> str:
        """Simulated flyctl for --mock: machine actions change the mock fleet."""
        await asyncio.sleep(0.2)
        if len(args) >= 5 and args[0] == "machine" and args[3] == "-a":
            action, machine_id, app = args[1], args[2], args[4]
            state = {"start": "started", "restart": "started", "stop": "stopped"}.get(action)
            if state:
                self.mock_fleet.set_state(app, machine_id, state)
        return ""
