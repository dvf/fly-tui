import asyncio
import json

import httpx
import pytest

from ftui.config import Account
from ftui.fleet import AppRef, FleetClient, Machine, auth_header

SECRET = "fm2_not_a_real_token"

RAW = {
    "id": "148ed106c62289",
    "name": "dry-fire-42",
    "state": "started",
    "region": "fra",
    "created_at": "2026-03-10T12:00:00Z",
    "updated_at": "2026-09-29T08:30:00.123Z",
    "image_ref": {"registry": "registry.fly.io", "repository": "acme-web",
                  "tag": "deployment-01K6", "digest": "sha256:abc"},
    "config": {
        "image": "registry.fly.io/acme-web:deployment-01K6",
        "guest": {"cpu_kind": "shared", "cpus": 2, "memory_mb": 1024},
        "metadata": {"fly_process_group": "web"},
        "services": [{"min_machines_running": 1}, {"min_machines_running": 2}],
    },
    "checks": [{"name": "a", "status": "passing"}, {"name": "b", "status": "critical"}],
}


def test_normalise_machine():
    m = Machine.from_api("acme", "acme-prod", "acme-web", RAW)
    assert (m.account, m.org, m.app, m.id, m.name) == (
        "acme", "acme-prod", "acme-web", "148ed106c62289", "dry-fire-42")
    assert m.state == "started" and m.region == "fra"
    assert (m.cpu_kind, m.cpus, m.memory_mb) == ("shared", 2, 1024)
    assert m.size == "shared-2x 1GB"
    assert m.image == "acme-web:deployment-01K6"
    assert m.process_group == "web"
    assert m.created_at == "2026-03-10 12:00:00"
    assert m.updated_at == "2026-09-29 08:30:00"
    assert m.checks == "1/2"
    assert m.min_running == 2
    assert m.key == "acme/acme-web/148ed106c62289"


def test_normalise_sparse_machine():
    m = Machine.from_api("a", "o", "app", {"id": "x", "config": {"image": "flyio/hellofly:latest"}})
    assert m.image == "latest"
    assert m.size == "-"
    assert m.checks == "" and m.min_running == 0


def test_auth_header():
    assert auth_header("fm2_abc") == "FlyV1 fm2_abc"
    assert auth_header("FlyV1 fm2_abc") == "FlyV1 fm2_abc"
    assert auth_header("legacy") == "Bearer legacy"


def run(coro):
    return asyncio.run(coro)


def test_parallel_fetch_with_one_failing_app():
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        assert request.headers["authorization"] == f"FlyV1 {SECRET}"
        app = request.url.path.split("/")[3]
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        if app == "broken":
            return httpx.Response(500, json={"error": "boom"})
        if app == "slow":
            await asyncio.sleep(5)
        return httpx.Response(200, content=json.dumps([dict(RAW, id=f"{app}-1")]))

    account = Account(name="acct", token_source="env:X", token=SECRET)
    apps = [AppRef("acct", "org", f"app{i}") for i in range(20)]
    apps += [AppRef("acct", "org", "broken"), AppRef("acct", "org", "slow")]

    async def go():
        fleet = FleetClient([account], concurrency=4, app_timeout=0.5,
                            transport=httpx.MockTransport(handler))
        try:
            return await fleet.list_machines(apps)
        finally:
            await fleet.aclose()

    machines, errors = run(go())
    assert len(machines) == 20
    assert {m.app for m in machines} == {f"app{i}" for i in range(20)}
    assert set(errors) == {("acct", "broken"), ("acct", "slow")}
    assert "500" in errors[("acct", "broken")]
    assert peak <= 4
    assert all(SECRET not in e for e in errors.values())


def test_list_apps_graphql_with_org_filter_and_account_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in request.headers["authorization"]:
            return httpx.Response(401, json={"errors": [{"message": "unauthorized"}]})
        return httpx.Response(200, json={"data": {"organizations": {"nodes": [
            {"slug": "acme-prod", "apps": {"nodes": [{"name": "acme-web", "status": "deployed"}]}},
            {"slug": "personal", "apps": {"nodes": [{"name": "toy", "status": "deployed"}]}},
        ]}}})

    good = Account(name="good", token_source="env:X", orgs=("acme-prod",), token="fm2_good")
    bad = Account(name="bad", token_source="env:Y", token="fm2_bad")

    async def go():
        fleet = FleetClient([good, bad], transport=httpx.MockTransport(handler))
        try:
            return await fleet.list_apps()
        finally:
            await fleet.aclose()

    apps, errors = run(go())
    assert apps == [AppRef("good", "acme-prod", "acme-web", "deployed")]
    assert "401" in errors["bad"] and "fm2_bad" not in errors["bad"]


def test_list_apps_falls_back_to_rest_for_org_tokens():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            return httpx.Response(403, json={})
        assert request.url.params["org_slug"] == "acme-prod"
        return httpx.Response(200, json={"apps": [{"name": "acme-web", "status": "deployed"}]})

    acct = Account(name="a", token_source="env:X", orgs=("acme-prod",), token="fm2_x")

    async def go():
        fleet = FleetClient([acct], transport=httpx.MockTransport(handler))
        try:
            return await fleet.list_apps()
        finally:
            await fleet.aclose()

    apps, errors = run(go())
    assert [a.name for a in apps] == ["acme-web"] and not errors
