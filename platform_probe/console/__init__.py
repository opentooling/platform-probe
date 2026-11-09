"""The probe's web UI and its JSON API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from aiohttp import web

from ..experiments import experiments, run_experiment
from ..junit import junit
from ..runner import Probe

WEB = Path(__file__).parent / "web"
log = logging.getLogger(__name__)

PROBE = web.AppKey("probe", Probe)
RUN = web.AppKey("run", dict)


def json_response(status: int, body) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(body) + "\n",
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def app(probe: Probe) -> web.Application:
    a = web.Application()
    a[PROBE] = probe
    # The task of the current run. It belongs to the app, not to the request that started it,
    # so closing the browser doesn't stop a run.
    a[RUN] = {"task": None}
    r = a.router
    r.add_get("/healthz", healthz)
    r.add_get("/api/config", config)
    r.add_get("/api/report", report)
    r.add_get("/api/report.junit", report_junit)
    r.add_post("/api/run", run)
    r.add_get("/api/experiments", list_experiments)
    r.add_post("/api/experiments/{name}", experiment)
    r.add_get("/", static(WEB / "index.html"))
    for f in sorted(WEB.iterdir()):
        if f.is_file():
            r.add_get("/" + f.name, static(f))
    a.on_cleanup.append(_cleanup)
    return a


def static(path: Path):
    async def handler(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(path)

    return handler


async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def report(request: web.Request) -> web.Response:
    return json_response(200, request.app[PROBE].report().to_dict())


async def list_experiments(request: web.Request) -> web.Response:
    return json_response(200, [e.to_dict() for e in experiments(request.app[PROBE])])


async def _cleanup(a: web.Application) -> None:
    task = a[RUN]["task"]
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await a[PROBE].close()


async def config(request: web.Request) -> web.Response:
    """What the header shows: where the probe points, never credentials."""
    p = request.app[PROBE]
    c = p.cfg
    e = c.expect
    return json_response(
        200,
        {
            "release": c.release,
            "namespace": c.namespace,
            "gateway": {
                "name": c.gateway.name,
                "namespace": c.gateway.namespace,
                "host": c.gateway.host,
                "address": c.gateway.address,
            },
            "limits": {
                "public": c.limits.public.to_json(),
                "free": c.limits.free.to_json(),
                "gold": c.limits.gold.to_json(),
            },
            "split": {"gateway": c.routes.split_v2_percent, "mesh": c.mesh.split_v2_percent},
            "expect": {
                "defaultDeny": e.default_deny,
                "httpRedirect": e.http_redirect,
                "strictMTLS": e.strict_mtls,
                "authorizationPolicies": e.authorization_policies,
                "trafficManagement": e.traffic_management,
                "networkPolicies": e.network_policies,
            },
            "oidc": c.oidc.enabled,
            "checks": [{"id": ch.id, "group": ch.group, "name": ch.name} for ch in p.checks],
        },
    )


async def run(request: web.Request) -> web.Response:
    """Start a run in the background. With ?wait=true, answer when the run is done, with the
    report (that is what `platform-probe check` and `helm test` use)."""
    p = request.app[PROBE]
    if not p.begin():
        return json_response(409, {"error": "a run is already in progress"})
    task = asyncio.create_task(p.execute())
    request.app[RUN]["task"] = task
    task.add_done_callback(_log_failure)
    if request.query.get("wait") != "true":
        return json_response(202, p.report().to_dict())
    # Shielded: a client that disconnects stops waiting, the run goes on.
    await asyncio.shield(task)
    return json_response(200, p.report().to_dict())


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("run failed", exc_info=task.exception())


async def report_junit(request: web.Request) -> web.Response:
    return web.Response(
        body=junit(request.app[PROBE].report()),
        content_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="platform-probe-junit.xml"'},
    )


async def experiment(request: web.Request) -> web.Response:
    try:
        n = int(request.query.get("n", "0"))
    except ValueError:
        n = 0
    try:
        async with asyncio.timeout(120):
            res = await run_experiment(request.app[PROBE], request.match_info["name"], n or 20)
    except TimeoutError:
        return json_response(504, {"error": "the experiment took longer than 2 minutes"})
    if res is None:
        return json_response(404, {"error": "no such experiment"})
    return json_response(200, res.to_dict())
