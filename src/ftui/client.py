import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Machine:
    """Represents a Fly.io Machine instance."""
    id: str
    name: str
    state: str
    region: str
    image: str
    process_group: str
    cpu: str
    ram: str
    has_http: bool
    created_at: str
    # The machine's config.env (non-secret), sorted. Used by the inspect panel.
    env: Tuple[Tuple[str, str], ...] = field(default=(), compare=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Machine":
        config = data.get("config", {})
        guest = config.get("guest", {})
        metadata = config.get("metadata", {})
        services = config.get("services", [])

        cpu_kind = guest.get("cpu_kind", "")
        cpus = guest.get("cpus", "")
        cpu_str = f"{cpu_kind} {cpus}x" if cpu_kind and cpus else "-"
        
        ram_mb = guest.get("memory_mb", "")
        ram_str = f"{ram_mb} MB" if ram_mb else "-"
        
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            state=data.get("state", ""),
            region=data.get("region", ""),
            image=config.get("image") or data.get("image", "-"),
            process_group=metadata.get("fly_process_group", "-"),
            cpu=cpu_str,
            ram=ram_str,
            has_http=any(s.get("protocol") == "tcp" for s in services),
            created_at=data.get("created_at", "")[:19].replace("T", " "),
            env=tuple(sorted(
                (str(k), "" if v is None else str(v)) for k, v in (config.get("env") or {}).items()
            )),
        )


class FlyError(Exception):
    """Base exception for Fly.io operations."""
    pass


class FlyClient:
    """Client for interacting with the Fly.io CLI."""

    def __init__(self, mock: bool = False, timeout: float = 5.0, refresh_interval: int = 5):
        self.mock = mock or os.getenv("FTUI_MOCK") == "1"
        self.timeout = timeout
        self.refresh_interval = refresh_interval
        self._bin = shutil.which("fly") or shutil.which("flyctl")

    async def _exec(self, *args: str) -> str:
        """Execute a flyctl command and return stdout."""
        if self.mock:
            return await self._mock_exec(*args)

        if not self._bin:
            raise FlyError("flyctl not found in PATH")

        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), 
                timeout=self.timeout
            )
            
            if proc.returncode != 0:
                raise FlyError(stderr.decode().strip() or f"Command failed with code {proc.returncode}")
                
            return stdout.decode()
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise FlyError(f"Command timed out: fly {' '.join(args)}")
        except Exception as e:
            raise FlyError(str(e))

    async def get_app_name(self) -> str:
        """Retrieve the app name from the current context."""
        try:
            output = await self._exec("status", "--json")
            return json.loads(output).get("Name", "Unknown App")
        except FlyError:
            return os.getenv("FLY_APP", "Unknown App")

    async def list_machines(self) -> List[Machine]:
        """List all machines for the current app."""
        output = await self._exec("machines", "list", "--json")
        data = json.loads(output) if output.strip() else []
        machines = [Machine.from_dict(m) for m in data]
        return sorted(machines, key=lambda m: m.created_at, reverse=True)

    async def restart_machine(self, machine_id: str) -> None:
        await self._exec("machine", "restart", machine_id, "--yes")

    async def stop_machine(self, machine_id: str) -> None:
        await self._exec("machine", "stop", machine_id)

    async def start_machine(self, machine_id: str) -> None:
        await self._exec("machine", "start", machine_id)

    async def scale_count(self, count: int) -> None:
        await self._exec("scale", "count", str(count), "--yes")

    async def scale_vm(self, size: str) -> None:
        await self._exec("scale", "vm", size, "--yes")

    async def _mock_exec(self, *args: str) -> str:
        """Simulated flyctl responses for local development."""
        await asyncio.sleep(0.2)
        cmd = " ".join(args)

        if "status" in cmd:
            return json.dumps({"Name": "mock-app-production"})
        
        if "machines list" in cmd:
            return json.dumps([
                {
                    "id": "148ed106c62289",
                    "name": "dry-fire-42",
                    "state": "started",
                    "region": "ams",
                    "created_at": "2024-03-10T12:00:00Z",
                    "config": {
                        "image": "flyio/hellofly:latest",
                        "metadata": {"fly_process_group": "app"},
                        "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 256},
                        "services": [{"protocol": "tcp"}],
                        "env": {"APP_ENV": "production", "LOG_LEVEL": "info", "PORT": "8080"},
                    }
                },
                {
                    "id": "7811d615b04489",
                    "name": "vocal-water-12",
                    "state": "stopped",
                    "region": "ord",
                    "created_at": "2024-03-11T10:00:00Z",
                    "config": {
                        "image": "flyio/hellofly:latest",
                        "metadata": {"fly_process_group": "worker"},
                        "guest": {"cpu_kind": "performance", "cpus": 2, "memory_mb": 1024},
                        "services": [],
                        "env": {"APP_ENV": "production", "LOG_LEVEL": "info", "QUEUE": "default"},
                    }
                }
            ])
        return ""
