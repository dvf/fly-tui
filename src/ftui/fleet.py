"""Async data layer: every machine across several Fly accounts, orgs and apps.

Reads go straight to the Fly HTTP APIs with httpx rather than through flyctl:

* orgs and apps come from one GraphQL query per account
  (`organizations { apps }`), which is a single round trip where flyctl would
  need `fly orgs list` plus one `fly apps list -o ORG` per org, each a
  subprocess costing a few hundred milliseconds. If GraphQL fails (e.g. an
  org-scoped deploy token) and the account lists its `orgs`, we fall back to
  the Machines API `GET /v1/apps?org_slug=ORG`.
* machines come from the Machines API `GET /v1/apps/{app}/machines`, fetched
  in parallel under a concurrency limit, each with its own timeout, so one
  slow or broken app never blocks the rest.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

from ftui.config import Account, ConfigError, resolve_token

GRAPHQL_URL = "https://api.fly.io/graphql"
MACHINES_URL = "https://api.machines.dev/v1"

ORGS_AND_APPS_QUERY = """
query {
  organizations {
    nodes {
      slug
      apps(first: 500) { nodes { name status } }
    }
  }
}
"""


class FleetError(Exception):
    pass


def auth_header(token: str) -> str:
    """Authorization header value for a Fly token (macaroon or legacy)."""
    if token.startswith("FlyV1 "):
        return token
    if token.startswith(("fm1", "fm2")):
        return f"FlyV1 {token}"
    return f"Bearer {token}"


def _ts(value: Optional[str]) -> str:
    return (value or "")[:19].replace("T", " ")


@dataclass(frozen=True)
class Machine:
    """One Fly Machine, normalised, tagged with where it lives."""
    account: str
    org: str
    app: str
    id: str
    name: str
    state: str
    region: str
    cpu_kind: str
    cpus: int
    memory_mb: int
    image: str
    process_group: str
    created_at: str
    updated_at: str
    checks: str = ""
    min_running: int = 0
    # The machine's config.env (non-secret), sorted. Used by the inspect panel.
    env: Tuple[Tuple[str, str], ...] = field(default=(), compare=False)

    @property
    def key(self) -> str:
        return f"{self.account}/{self.app}/{self.id}"

    @property
    def size(self) -> str:
        if not self.cpu_kind:
            return "-"
        mem = f"{self.memory_mb // 1024}GB" if self.memory_mb and self.memory_mb % 1024 == 0 \
            else f"{self.memory_mb}MB"
        return f"{self.cpu_kind}-{self.cpus}x {mem}"

    def search_text(self) -> str:
        return " ".join((
            self.account, self.org, self.app, self.id, self.name, self.state,
            self.region, self.size, self.image, self.process_group,
        )).lower()

    @classmethod
    def from_api(cls, account: str, org: str, app: str, data: dict) -> "Machine":
        config = data.get("config") or {}
        guest = config.get("guest") or {}
        metadata = config.get("metadata") or {}
        image_ref = data.get("image_ref") or {}

        image = image_ref.get("tag") or ""
        if not image:
            ref = config.get("image") or data.get("image") or ""
            image = ref.rsplit(":", 1)[-1] if ":" in ref.rsplit("/", 1)[-1] else ref
        if image_ref.get("tag") and image_ref.get("repository"):
            image = f"{image_ref['repository'].rsplit('/', 1)[-1]}:{image_ref['tag']}"

        checks = data.get("checks") or []
        passing = sum(1 for c in checks if c.get("status") == "passing")
        checks_str = f"{passing}/{len(checks)}" if checks else ""

        min_running = max(
            (s.get("min_machines_running") or 0 for s in config.get("services") or []),
            default=0,
        )

        return cls(
            account=account,
            org=org,
            app=app,
            id=data.get("id", ""),
            name=data.get("name", ""),
            state=data.get("state", ""),
            region=data.get("region", ""),
            cpu_kind=guest.get("cpu_kind", ""),
            cpus=int(guest.get("cpus") or 0),
            memory_mb=int(guest.get("memory_mb") or 0),
            image=image or "-",
            process_group=metadata.get("fly_process_group", "-"),
            created_at=_ts(data.get("created_at")),
            updated_at=_ts(data.get("updated_at")),
            checks=checks_str,
            min_running=int(min_running),
            env=tuple(sorted(
                (str(k), "" if v is None else str(v)) for k, v in (config.get("env") or {}).items()
            )),
        )


@dataclass(frozen=True)
class AppRef:
    account: str
    org: str
    name: str
    status: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return (self.account, self.name)


@dataclass
class Snapshot:
    """The result of a refresh, including partial failures."""
    apps: List[AppRef] = field(default_factory=list)
    machines: List[Machine] = field(default_factory=list)
    # account name -> error message (token or app listing failed)
    account_errors: Dict[str, str] = field(default_factory=dict)
    # (account, app) -> error message (machines fetch failed)
    app_errors: Dict[Tuple[str, str], str] = field(default_factory=dict)


class FleetClient:
    """Reads orgs, apps and machines for a set of accounts."""

    def __init__(
        self,
        accounts: Iterable[Account],
        concurrency: int = 8,
        app_timeout: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.accounts: Dict[str, Account] = {a.name: a for a in accounts}
        self.concurrency = concurrency
        self.app_timeout = app_timeout
        self._http = httpx.AsyncClient(timeout=app_timeout, transport=transport)
        self._sem = asyncio.Semaphore(concurrency)
        self.account_errors: Dict[str, str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- tokens -----------------------------------------------------------

    async def resolve_tokens(self) -> None:
        """Read every account's token once. Failures mark just that account."""
        async def one(account: Account) -> None:
            if account.token:
                return
            try:
                account.token = await asyncio.to_thread(resolve_token, account.token_source)
            except ConfigError as e:
                self.account_errors[account.name] = str(e)

        await asyncio.gather(*(one(a) for a in self.accounts.values()))

    def _headers(self, account: Account) -> dict:
        return {"Authorization": auth_header(account.token or "")}

    # -- orgs and apps ------------------------------------------------------

    async def _apps_graphql(self, account: Account) -> List[AppRef]:
        resp = await self._http.post(
            GRAPHQL_URL,
            json={"query": ORGS_AND_APPS_QUERY},
            headers=self._headers(account),
        )
        if resp.status_code != 200:
            raise FleetError(f"GraphQL HTTP {resp.status_code}")
        body = resp.json()
        if body.get("errors") and not body.get("data"):
            raise FleetError(body["errors"][0].get("message", "GraphQL error"))
        apps = []
        for org in ((body.get("data") or {}).get("organizations") or {}).get("nodes") or []:
            for app in (org.get("apps") or {}).get("nodes") or []:
                apps.append(AppRef(account.name, org["slug"], app["name"], app.get("status") or ""))
        return apps

    async def _apps_rest(self, account: Account) -> List[AppRef]:
        apps = []
        for org in account.orgs:
            resp = await self._http.get(
                f"{MACHINES_URL}/apps", params={"org_slug": org}, headers=self._headers(account)
            )
            if resp.status_code != 200:
                raise FleetError(f"apps list for {org}: HTTP {resp.status_code}")
            for app in resp.json().get("apps") or []:
                apps.append(AppRef(account.name, org, app["name"], app.get("status") or ""))
        return apps

    async def list_account_apps(self, account: Account) -> List[AppRef]:
        try:
            apps = await self._apps_graphql(account)
        except (FleetError, httpx.HTTPError, ValueError) as e:
            if not account.orgs:
                raise FleetError(str(e) or type(e).__name__)
            apps = await self._apps_rest(account)
        if account.orgs:
            apps = [a for a in apps if a.org in account.orgs]
        apps = [a for a in apps if account.shows_app(a.name)]
        return sorted(apps, key=lambda a: (a.org, a.name))

    async def list_apps(self) -> Tuple[List[AppRef], Dict[str, str]]:
        """All apps across accounts, plus per-account errors."""
        errors: Dict[str, str] = dict(self.account_errors)

        async def one(account: Account) -> List[AppRef]:
            if account.name in self.account_errors:
                return []
            try:
                return await self.list_account_apps(account)
            except (FleetError, httpx.HTTPError) as e:
                errors[account.name] = str(e) or type(e).__name__
                return []

        results = await asyncio.gather(*(one(a) for a in self.accounts.values()))
        return [app for apps in results for app in apps], errors

    # -- machines -----------------------------------------------------------

    async def list_app_machines(self, app: AppRef) -> List[Machine]:
        account = self.accounts[app.account]
        async with self._sem:
            resp = await asyncio.wait_for(
                self._http.get(
                    f"{MACHINES_URL}/apps/{app.name}/machines", headers=self._headers(account)
                ),
                timeout=self.app_timeout,
            )
        if resp.status_code != 200:
            raise FleetError(f"HTTP {resp.status_code}")
        return [Machine.from_api(app.account, app.org, app.name, m) for m in resp.json() or []]

    async def list_machines(
        self, apps: Iterable[AppRef]
    ) -> Tuple[List[Machine], Dict[Tuple[str, str], str]]:
        """Fetch machines for every app in parallel; one failing app marks only itself."""
        apps = list(apps)
        errors: Dict[Tuple[str, str], str] = {}

        async def one(app: AppRef) -> List[Machine]:
            try:
                return await self.list_app_machines(app)
            except asyncio.TimeoutError:
                errors[app.key] = "timed out"
            except (FleetError, httpx.HTTPError, ValueError) as e:
                errors[app.key] = str(e) or type(e).__name__
            return []

        results = await asyncio.gather(*(one(a) for a in apps))
        machines = [m for ms in results for m in ms]
        machines.sort(key=lambda m: (m.account, m.org, m.app, m.process_group, m.created_at))
        return machines, errors
