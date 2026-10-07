"""Simulated Fly fleet for `ftui --mock`: several accounts, orgs and apps.

The mock sits at the HTTP transport layer, so mock mode runs the same
fetching, parsing and error handling code as the real thing.
"""

import json
import zlib
from typing import Dict, List, Tuple

import httpx

from ftui.config import Account

# account -> org -> app -> list of (id, name, state, region, cpu_kind, cpus, mem, group)
_FLEET = {
    "acme": {
        "acme-prod": {
            "acme-web": [
                ("148ed106c62289", "dry-fire-42", "started", "ams", "shared", 1, 512, "app"),
                ("7811d615b04489", "vocal-water-12", "started", "fra", "shared", 1, 512, "app"),
                ("3d8d9e1a2b3c44", "quiet-sun-7", "stopped", "ord", "shared", 1, 512, "app"),
            ],
            "acme-worker": [
                ("91857e4f6a2d18", "bold-leaf-3", "started", "fra", "performance", 2, 4096, "worker"),
            ],
            "acme-legacy-cron": [],  # always fails to fetch: shows per-app errors
        },
        "acme-staging": {
            "acme-web-staging": [
                ("e2865d3b7c9f01", "shy-moon-55", "stopped", "ams", "shared", 1, 256, "app"),
            ],
        },
    },
    "globex": {  # read_only in the mock accounts
        "globex": {
            "globex-api": [
                ("5683d9f2a1c7e0", "calm-river-9", "started", "iad", "shared", 2, 1024, "app"),
                ("0e2865d3b7c911", "red-hill-21", "stopped", "iad", "shared", 2, 1024, "app"),
            ],
            "globex-db": [
                ("d8963e1f4b2a77", "old-tree-88", "started", "iad", "performance", 4, 8192, "app"),
            ],
        },
    },
    "initech": {
        "initech": {
            "tps-reports": [
                ("a1b2c3d4e5f607", "tidy-cloud-1", "stopped", "lhr", "shared", 1, 256, "app"),
                ("b1c2d3e4f5a608", "tidy-cloud-2", "stopped", "lhr", "shared", 1, 256, "app"),
            ],
        },
    },
}

# Apps whose services declare min_machines_running (to show the warning marker).
_MIN_RUNNING = {"acme-web": 3, "tps-reports": 1}
_BROKEN_APPS = {"acme-legacy-cron"}


def mock_accounts() -> List[Account]:
    return [
        Account(name=name, token_source="mock", read_only=(name == "globex"), token=f"mock-{name}")
        for name in _FLEET
    ]


class MockFleet:
    """Mutable in-memory fleet shared by the HTTP mock and the flyctl mock."""

    def __init__(self):
        self.machines: Dict[str, List[dict]] = {}
        self.orgs: Dict[str, Dict[str, List[str]]] = {}
        for account, orgs in _FLEET.items():
            self.orgs[account] = {}
            for org, apps in orgs.items():
                self.orgs[account][org] = list(apps)
                for app, rows in apps.items():
                    self.machines[app] = [self._machine(app, *row) for row in rows]

    @staticmethod
    def _machine(app, mid, name, state, region, cpu_kind, cpus, mem, group) -> dict:
        services = []
        if app in _MIN_RUNNING:
            services = [{"protocol": "tcp", "min_machines_running": _MIN_RUNNING[app]}]
        return {
            "id": mid,
            "name": name,
            "state": state,
            "region": region,
            "created_at": "2026-03-10T12:00:00Z",
            "updated_at": "2026-09-29T08:30:00Z",
            "image_ref": {"repository": f"registry.fly.io/{app}", "tag": "deployment-01K6"},
            "config": {
                "image": f"registry.fly.io/{app}:deployment-01K6",
                "metadata": {"fly_process_group": group},
                "guest": {"cpu_kind": cpu_kind, "cpus": cpus, "memory_mb": mem},
                "services": services,
                "env": mock_env(app),
            },
            "checks": [{"name": "http", "status": "passing" if state == "started" else "critical"}],
        }

    def set_state(self, app: str, machine_id: str, state: str) -> None:
        for m in self.machines.get(app, []):
            if m["id"] == machine_id:
                m["state"] = state

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.fly.io" and url.path == "/graphql":
            # mock tokens are "mock-<account>", sent as "Bearer mock-<account>"
            account = request.headers.get("authorization", "").partition("mock-")[2]
            return self._graphql(account)
        parts = url.path.strip("/").split("/")
        # /v1/apps/{app}/machines
        if len(parts) == 4 and parts[1] == "apps" and parts[3] == "machines":
            app = parts[2]
            if app in _BROKEN_APPS:
                return httpx.Response(500, json={"error": "internal error"})
            if app not in self.machines:
                return httpx.Response(404, json={"error": "app not found"})
            return httpx.Response(200, content=json.dumps(self.machines[app]))
        return httpx.Response(404, json={"error": "not found"})

    def _graphql(self, account: str) -> httpx.Response:
        orgs = self.orgs.get(account, {})
        nodes = [
            {"slug": org, "apps": {"nodes": [{"name": a, "status": "deployed"} for a in apps]}}
            for org, apps in orgs.items()
        ]
        return httpx.Response(200, json={"data": {"organizations": {"nodes": nodes}}})


# -- inspect panel (`c`): deployed config, env and secret metadata --------------
#
# Secrets carry names, digests and status only, like `fly secrets list --json`.
# There are no values here, and the inspector never asks for any.

_ENV = {
    "acme-web": {"APP_ENV": "production", "LOG_LEVEL": "info", "PORT": "8080",
                 "PRIMARY_REGION": "ams", "FEATURE_CHECKOUT_V2": "true"},
    "acme-web-staging": {"APP_ENV": "staging", "LOG_LEVEL": "debug", "PORT": "8080",
                         "PRIMARY_REGION": "ams", "SEED_DEMO_DATA": "1"},
    "acme-worker": {"APP_ENV": "production", "QUEUE": "default", "CONCURRENCY": "8"},
    "globex-api": {"APP_ENV": "production", "PORT": "3000", "PRIMARY_REGION": "iad"},
    "globex-db": {"PRIMARY_REGION": "iad"},
    "tps-reports": {"APP_ENV": "production", "COVER_SHEET": "required"},
    "mock-app-production": {"APP_ENV": "production", "LOG_LEVEL": "info", "PORT": "8080"},
    "mock-app-staging": {"APP_ENV": "staging", "LOG_LEVEL": "debug", "PORT": "8080",
                         "SEED_DEMO_DATA": "1"},
}

# app -> [(name, digest, status)]. Matching digests across apps mean the same value.
_SECRETS = {
    "acme-web": [
        ("DATABASE_URL", "a1f0c3d9e2b47781", "Deployed"),
        ("SESSION_SECRET", "5be2a0917c3d4e10", "Deployed"),
        ("PAYMENTS_API_KEY", "9c4e1b7a2d5f3086", "Deployed"),
        ("MAILER_TOKEN", "e7d2c9a41b0f5638", "Deployed"),
    ],
    "acme-web-staging": [
        ("DATABASE_URL", "0d3b8e6fa9c21457", "Deployed"),
        ("SESSION_SECRET", "c81f4a2e9b7d0365", "Deployed"),
        ("PAYMENTS_API_KEY", "9c4e1b7a2d5f3086", "Deployed"),  # same as acme-web
        ("DEBUG_TOOLBAR_TOKEN", "4a6e0c8b2f1d9753", "Staged"),
    ],
    "acme-worker": [
        ("DATABASE_URL", "a1f0c3d9e2b47781", "Deployed"),
        ("QUEUE_URL", "7f2b9d4c1e8a6035", "Deployed"),
    ],
    "globex-api": [
        ("DATABASE_URL", "3e9a7c1f5b2d8046", "Deployed"),
        ("JWT_SIGNING_KEY", "b6d1e8f03a9c2574", "Partial"),
    ],
    "tps-reports": [("PRINTER_TOKEN", "2c7e5a9d0f3b8164", "Deployed")],
    "mock-app-production": [
        ("DATABASE_URL", "f3a8c1e7d2b94056", "Deployed"),
        ("API_KEY", "6d0b4f9e2a7c1583", "Deployed"),
    ],
    "mock-app-staging": [
        ("DATABASE_URL", "81c5e2a9f0d7b346", "Deployed"),
        ("API_KEY", "6d0b4f9e2a7c1583", "Deployed"),  # same as production
    ],
}

# (app, what) -> error, to show per-tab error handling offline.
_INSPECT_ERRORS = {
    ("globex-db", "secrets"): "Not authorized to access secrets for this app",
    ("acme-legacy-cron", "config"): "Could not find App",
    ("acme-legacy-cron", "secrets"): "Could not find App",
    ("acme-legacy-cron", "machines"): "Could not find App",
}

# Apps the single-app mock (FTUI_MOCK=1) can see, for the diff picker.
MOCK_SINGLE_APPS = ("mock-app-production", "mock-app-staging")


def mock_env(app: str) -> Dict[str, str]:
    return dict(_ENV.get(app, {}))


def _mock_config(app: str) -> dict:
    env = mock_env(app)
    region = env.get("PRIMARY_REGION", "ams")
    return {
        "app": app,
        "primary_region": region,
        "build": {"image": f"registry.fly.io/{app}:deployment-01K6"},
        "deploy": {"strategy": "rolling"},
        "env": env,
        "http_service": {
            "internal_port": int(env.get("PORT", "8080")),
            "force_https": True,
            "auto_stop_machines": "stop",
            "min_machines_running": _MIN_RUNNING.get(app, 0),
            "checks": [{"grace_period": "10s", "interval": "30s", "method": "GET", "path": "/health"}],
        },
        "vm": [{"size": "shared-cpu-1x", "memory": "512mb"}],
    }


def mock_inspect(app: str, what: str):
    """Canned `fly config show` / `secrets list --json` / `machines list --json` data."""
    from ftui.client import FlyError

    if (app, what) in _INSPECT_ERRORS:
        raise FlyError(_INSPECT_ERRORS[(app, what)])
    if what == "config":
        return _mock_config(app)
    if what == "secrets":
        return [{"name": n, "digest": d, "status": s} for n, d, s in _SECRETS.get(app, [])]
    if what == "machines":
        return [{"id": f"{zlib.crc32(app.encode()):014x}",
                 "config": {"env": mock_env(app), "metadata": {"fly_process_group": "app"}}}]
    raise ValueError(what)
