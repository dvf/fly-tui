# Deprecated: an older copy of ftui.client, kept so existing imports keep working.
import json
import asyncio
import os
import random
from typing import List, Dict, Any, Optional

class FlyClient:
    """Wrapper for flyctl commands."""
    
    def __init__(self, mock: bool = False):
        self.mock = mock or os.getenv("FTUI_MOCK") == "1"

    async def _run(self, args: List[str]) -> str:
        """Run a flyctl command and return its output with a timeout."""
        if self.mock:
            return await self._mock_run(args)
        
        try:
            proc = await asyncio.create_subprocess_exec(
                "fly", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            # Use wait_for to implement a timeout
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            
            if proc.returncode != 0:
                raise Exception(f"flyctl error: {stderr.decode()}")
            
            return stdout.decode()
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except:
                pass
            raise Exception(f"flyctl command timed out: fly {' '.join(args)}")

    async def _mock_run(self, args: List[str]) -> str:
        """Simulate flyctl command outputs."""
        await asyncio.sleep(0.1)  # Simulate network latency
        
        cmd = " ".join(args)
        if "machines list" in cmd and "--json" in cmd:
            return json.dumps([
                {
                    "id": "148ed106c62289",
                    "name": "gentle-sun-42",
                    "state": "started",
                    "region": "ams",
                    "image": "flyio/hellofly:latest",
                    "created_at": "2023-10-01T12:00:00Z",
                    "config": {
                        "metadata": {"fly_process_group": "app"},
                        "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 256},
                        "services": [{"protocol": "tcp", "internal_port": 8080}]
                    }
                },
                {
                    "id": "7811d615b04489",
                    "name": "bitter-cloud-12",
                    "state": "stopped",
                    "region": "ams",
                    "image": "flyio/hellofly:latest",
                    "created_at": "2023-10-02T10:00:00Z",
                    "config": {
                        "metadata": {"fly_process_group": "worker"},
                        "guest": {"cpu_kind": "shared", "cpus": 2, "memory_mb": 512},
                        "services": []
                    }
                }
            ])
        
        if "scale show" in cmd and "--json" in cmd:
            return json.dumps({
                "ProcessGroup": "app",
                "Count": 3,
                "CPUs": 1,
                "Memory": 256,
                "VMSize": "shared-cpu-1x"
            })
            
        return ""

    async def get_app_name(self) -> str:
        """Get the current application name."""
        if self.mock:
            return "mock-app-123"
        try:
            # Try to get app name from fly status
            output = await self._run(["status", "--json"])
            data = json.loads(output)
            return data.get("Name", "Unknown App")
        except:
            return os.getenv("FLY_APP", "Unknown App")

    async def list_machines(self) -> List[Dict[str, Any]]:
        """List machines for the current app."""
        output = await self._run(["machines", "list", "--json"])
        if not output.strip():
            return []
        return json.loads(output)

    async def get_scale(self) -> Dict[str, Any]:
        """Get scaling information for the current app."""
        # Using machine list to infer count and VM size as 'scale show' can be complex to parse
        # but let's try 'scale show --json' if supported or mock it.
        try:
            output = await self._run(["scale", "show", "--json"])
            return json.loads(output)
        except:
            # Fallback for older flyctl versions or if json output is not available
            machines = await self.list_machines()
            return {"Count": len(machines), "VMSize": "unknown"}

    async def scale_count(self, count: int):
        """Scale the machine count."""
        await self._run(["scale", "count", str(count), "--yes"])

    async def scale_vm(self, size: str):
        """Scale the VM size."""
        await self._run(["scale", "vm", size, "--yes"])

    async def stop_machine(self, machine_id: str):
        """Stop a specific machine."""
        await self._run(["machine", "stop", machine_id])

    async def start_machine(self, machine_id: str):
        """Start a specific machine."""
        await self._run(["machine", "start", machine_id])

    async def restart_machine(self, machine_id: str):
        """Restart a specific machine."""
        await self._run(["machine", "restart", machine_id])
