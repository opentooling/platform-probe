"""Runs the checks: platform status read from the Kubernetes API, API gateway features exercised
through the gateway, and service mesh features exercised between the probe's own services.

A check is an async function that takes the Probe and returns an Outcome. Checks register
themselves with @check, in the order they run; see platform_probe/checks/.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import quote

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from multidict import CIMultiDict, CIMultiDictProxy

from .config import Config, Limit
from .kube import Kube
from .service import FetchResult, Hop, describe_error, read_body

log = logging.getLogger(__name__)


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


# Groups, in the order they run and are shown.
GROUP_RHCL = "Connectivity Link"
GROUP_GATEWAY = "API gateway"
GROUP_MESH = "Service mesh"
GROUP_INGRESS = "Ingress & certificates"
GROUP_WORKLOADS = "Workload security"
GROUP_STORAGE = "Storage"
GROUP_OBSERVABILITY = "Observability"
GROUP_ARGOCD = "Argo CD"
GROUP_METALLB = "MetalLB"
GROUP_PKI = "Internal CA"

# The connectivity suite (the three groups above) always runs; the others are switched on in
# the chart's suites.* values.
CONNECTIVITY = "connectivity"


@dataclass
class Outcome:
    """What a check function returns."""

    status: Status
    detail: str = ""
    hint: str = ""


def passed(detail: str) -> Outcome:
    return Outcome(Status.PASS, detail)


def skipped(detail: str) -> Outcome:
    return Outcome(Status.SKIP, detail)


def failed(detail: str, hint: str = "") -> Outcome:
    return Outcome(Status.FAIL, detail, hint)


CheckFn = Callable[["Probe"], Awaitable[Outcome]]


@dataclass
class Check:
    id: str
    group: str
    name: str
    run: CheckFn
    suite: str = CONNECTIVITY

    @property
    def category(self) -> str:
        """What the console and `helm test` run separately: the check ID's prefix, one per group
        (platform, gateway, mesh, ingress, workloads, storage, ...)."""
        return self.id.split("-", 1)[0]


_registry: list[Check] = []


def check(check_id: str, group: str, name: str, suite: str = CONNECTIVITY) -> Callable[[CheckFn], CheckFn]:
    """Register a check. Checks run in the order they are registered."""

    def register(fn: CheckFn) -> CheckFn:
        if any(c.id == check_id for c in _registry):
            raise ValueError(f"duplicate check id {check_id}")
        _registry.append(Check(check_id, group, name, fn, suite))
        return fn

    return register


def all_checks() -> list[Check]:
    from . import checks  # noqa: F401 - importing registers them

    return list(_registry)


@dataclass
class Result:
    id: str
    group: str
    name: str
    status: Status
    detail: str = ""
    hint: str = ""
    duration_ms: int = 0
    # When the check finished.
    at: datetime | None = None

    @property
    def category(self) -> str:
        return self.id.split("-", 1)[0]

    def to_dict(self) -> dict:
        d = {"id": self.id, "group": self.group, "name": self.name, "status": str(self.status)}
        if self.detail:
            d["detail"] = self.detail
        if self.hint:
            d["hint"] = self.hint
        d["durationMs"] = self.duration_ms
        if self.at:
            d["at"] = _timestamp(self.at)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Result:
        return cls(
            id=d.get("id", ""),
            group=d.get("group", ""),
            name=d.get("name", ""),
            status=Status(d.get("status", "skip")),
            detail=d.get("detail", ""),
            hint=d.get("hint", ""),
            duration_ms=int(d.get("durationMs", 0)),
            at=datetime.fromisoformat(d["at"].replace("Z", "+00:00")) if d.get("at") else None,
        )


def _timestamp(t: datetime | None) -> str:
    return t.isoformat().replace("+00:00", "Z") if t else ""


@dataclass
class Report:
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    results: list[Result] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    # The categories this run covers; empty = all of them.
    categories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {"running": self.running, "startedAt": _timestamp(self.started_at) or "0001-01-01T00:00:00Z"}
        if self.finished_at:
            d["finishedAt"] = _timestamp(self.finished_at)
        d.update(
            durationMs=self.duration_ms,
            results=[r.to_dict() for r in self.results],
            passed=self.passed,
            failed=self.failed,
            skipped=self.skipped,
            total=self.total,
            categories=self.categories,
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Report:
        return cls(
            running=bool(d.get("running")),
            duration_ms=int(d.get("durationMs", 0)),
            results=[Result.from_dict(r) for r in d.get("results") or []],
            passed=int(d.get("passed", 0)),
            failed=int(d.get("failed", 0)),
            skipped=int(d.get("skipped", 0)),
            total=int(d.get("total", 0)),
            categories=list(d.get("categories") or []),
        )


# --- HTTP ---------------------------------------------------------------------------------------


@dataclass
class Resp:
    """One request's outcome. hop is set when the answer came from one of the probe's services."""

    status: int = 0
    headers: CIMultiDictProxy[str] | CIMultiDict[str] = field(default_factory=CIMultiDict)
    body: str = ""
    hop: Hop | None = None
    dur: float = 0.0
    err: BaseException | None = None

    def header(self, name: str) -> str:
        return self.headers.get(name, "")

    def __str__(self) -> str:
        if self.err is not None:
            return "error: " + describe_error(self.err)
        return str(self.status)


async def do(session: aiohttp.ClientSession, method: str, url: str, headers: dict[str, str] | None = None) -> Resp:
    start = time.monotonic()
    try:
        async with session.request(method, url, headers=headers, allow_redirects=False) as resp:
            raw = await read_body(resp, 1 << 20)
            out = Resp(status=resp.status, headers=resp.headers)
    except Exception as e:  # noqa: BLE001 - a failed request is a result
        return Resp(err=e, dur=time.monotonic() - start)
    out.dur = time.monotonic() - start
    out.body = raw.decode("utf-8", "replace").strip()
    with contextlib.suppress(ValueError):
        out.hop = Hop.from_dict(json.loads(raw))
    return out


class GatewayResolver(AbstractResolver):
    """Sends the probe's hostname to the gateway's address, like curl --resolve, so the checks
    need no DNS record and always reach the gateway under test. TLS still uses the hostname."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.default = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET):
        g = self.cfg.gateway
        if host != g.host:
            return await self.default.resolve(host, port, family)
        port = {443: g.https_port, 80: g.http_port}.get(port, port)
        infos = await self.default.resolve(g.address, port, family)
        return [{**info, "hostname": host} for info in infos]

    async def close(self) -> None:
        await self.default.close()


def trusted_context(ca_file: str) -> ssl.SSLContext:
    """The system roots plus the configured CA bundle, if it is there."""
    ctx = ssl.create_default_context()
    if ca_file and os.path.exists(ca_file):
        try:
            ctx.load_verify_locations(ca_file)
        except (OSError, ssl.SSLError) as e:
            log.warning("CA bundle %s: %s", ca_file, e)
    return ctx


def wait_window(limit: Limit) -> Awaitable[None]:
    """Let a rate-limit window expire, so a limit check starts from a fresh counter."""
    return asyncio.sleep(limit.window + 1)


async def burst(n: int, send: Callable[[], Awaitable[Resp]]) -> list[int]:
    """Send n requests one after another and return their status codes."""
    return [(await send()).status for _ in range(n)]


def codes_string(codes: list[int]) -> str:
    return " ".join(map(str, codes))


def new_id() -> str:
    n, digits = time.time_ns(), "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


class ProbeError(Exception):
    pass


class Probe:
    """Holds the clients the checks use, and the latest report."""

    def __init__(self, cfg: Config, kube: Kube | None = None, checks: list[Check] | None = None):
        self.cfg = cfg
        self.kube = kube if kube is not None else Kube()
        self.checks = checks if checks is not None else [c for c in all_checks() if self.suite_enabled(c.suite)]
        # Per run: an ID that labels what the run creates, state checks share (the storage
        # checks reuse one PVC), and undo actions run when the run ends.
        self.run_id = ""
        self.state: dict = {}
        self._report = Report()
        self._run_checks: list[Check] = list(self.checks)
        self._latest: dict[str, Result] = {}
        self._gateway: aiohttp.ClientSession | None = None
        self._mesh: aiohttp.ClientSession | None = None
        self.token_value = ""
        self.token_expires = 0.0

    def suite_enabled(self, suite: str) -> bool:
        if suite == CONNECTIVITY:
            return True
        s = getattr(self.cfg.suites, suite, None)
        return bool(s and s.enabled)

    # Sessions are created on first use, inside the event loop.
    def gateway_ssl(self) -> ssl.SSLContext:
        ctx = trusted_context(self.cfg.ca_file)
        if self.cfg.gateway.insecure_skip_verify:  # opt-in
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @property
    def gateway(self) -> aiohttp.ClientSession:
        """Dials the gateway for the probe's hostname and verifies its certificate."""
        if self._gateway is None:
            self._gateway = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(
                    resolver=GatewayResolver(self.cfg), ssl=self.gateway_ssl(), limit_per_host=8, keepalive_timeout=20
                ),
            )
        return self._gateway

    @property
    def mesh(self) -> aiohttp.ClientSession:
        """Calls the probe's services directly; the console's sidecar makes those calls mTLS."""
        if self._mesh is None:
            self._mesh = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(
                    ssl=trusted_context(self.cfg.ca_file), limit_per_host=8, keepalive_timeout=20
                ),
            )
        return self._mesh

    async def close(self) -> None:
        for s in (self._gateway, self._mesh):
            if s is not None:
                await s.close()
        self._gateway = self._mesh = None
        await self.kube.close()

    # --- running ---

    def categories(self) -> list[dict[str, str]]:
        """The categories of the checks this probe runs, in run order: [{id, name}]."""
        seen: dict[str, str] = {}
        for c in self.checks:
            seen.setdefault(c.category, c.group)
        return [{"id": k, "name": v} for k, v in seen.items()]

    def begin(self, categories: list[str] | None = None) -> bool:
        """Start a new report, for the given categories or all of them. False if a run is
        already in progress; ValueError for a category this probe doesn't have."""
        if self._report.running:
            return False
        wanted = [c for c in categories or [] if c]
        unknown = sorted(set(wanted) - {c["id"] for c in self.categories()})
        if unknown:
            raise ValueError(f"no such category: {', '.join(unknown)}")
        self._run_checks = [c for c in self.checks if not wanted or c.category in wanted]
        self._report = Report(
            running=True, started_at=datetime.now(UTC), total=len(self._run_checks), categories=wanted
        )
        return True

    def latest(self) -> list[Result]:
        """The most recent result of every check, whichever run it came from, in run order."""
        return [self._latest[c.id] for c in self.checks if c.id in self._latest]

    async def execute(self) -> None:
        """Run every check in order, into the report begin() started."""
        rep = self._report
        self.run_id, self.state = new_id(), {}
        scratch = None
        checks = self._run_checks
        if self.cfg.write_suites() and any(c.suite != CONNECTIVITY for c in checks):
            from . import scratch
        try:
            if scratch:
                # Leftovers of a run that was interrupted. A failure here shows up in the checks.
                try:
                    await scratch.sweep(self)
                except Exception:
                    log.exception("cleaning up the scratch namespace")
            for c in checks:
                start = time.monotonic()
                out = await self._safe_run(c)
                ms = int((time.monotonic() - start) * 1000)
                res = Result(c.id, c.group, c.name, out.status, out.detail, out.hint, ms, datetime.now(UTC))
                rep.results.append(res)
                self._latest[c.id] = res
                if out.status == Status.PASS:
                    rep.passed += 1
                elif out.status == Status.FAIL:
                    rep.failed += 1
                else:
                    rep.skipped += 1
        finally:
            if scratch:
                try:
                    await scratch.sweep(self)
                except Exception:
                    log.exception("cleaning up the scratch namespace")
            rep.running = False
            rep.finished_at = datetime.now(UTC)
            rep.duration_ms = int((rep.finished_at - rep.started_at).total_seconds() * 1000)

    async def run(self, categories: list[str] | None = None) -> bool:
        """Run every check, or those of the given categories. Only one run at a time; a second
        call returns False."""
        if not self.begin(categories):
            return False
        await self.execute()
        return True

    async def _safe_run(self, c: Check) -> Outcome:
        try:
            return await c.run(self)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("check %s crashed", c.id)
            return failed(f"check crashed: {describe_error(e)}")

    def report(self) -> Report:
        """A copy of the current (or last) report."""
        return replace(self._report, results=list(self._report.results))

    # --- traffic ---

    async def gw(self, method: str, path: str, headers: dict[str, str] | None = None) -> Resp:
        """Send a request through the gateway to a path on the probe's hostname."""
        return await do(self.gateway, method, self.cfg.url(path), headers)

    async def call(self, base: str, path: str, headers: dict[str, str] | None = None) -> Resp:
        """Call one of the probe's services directly."""
        return await do(self.mesh, "GET", base.rstrip("/") + path, headers)

    async def front(self, headers: dict[str, str] | None = None) -> Resp:
        """Call the frontend directly from the console, so the whole chain stays inside the mesh."""
        return await self.call(self.cfg.services.frontend, "/", headers)

    async def from_outsider(self, target: str) -> FetchResult:
        """Ask the outsider (a pod with no sidecar) to call a URL."""
        r = await self.call(self.cfg.services.outsider, "/fetch?url=" + quote(target, safe=""))
        if r.err is not None:
            raise ProbeError(describe_error(r.err))
        try:
            data = json.loads(r.body)
        except ValueError:
            data = None
        if r.status != 200 or not isinstance(data, dict):
            raise ProbeError(f"outsider answered {r.status}: {r.body}")
        return FetchResult.from_dict(data)

    def api_key(self, user: str) -> dict[str, str]:
        return {"Authorization": f"{self.cfg.api_keys.prefix} {self.cfg.api_key(user)}"}


def hops(r: Resp) -> tuple[Hop | None, Hop | None, Hop | None]:
    """frontend, catalog and pricing from a chain, None where the chain stopped."""
    front = r.hop
    catalog = front.next if front else None
    pricing = catalog.next if catalog else None
    return front, catalog, pricing


def version(r: Resp) -> str:
    return r.hop.version if r.hop else ""


def catalog_version(r: Resp) -> str:
    _, c, _ = hops(r)
    return c.version if c else ""


def chain(r: Resp) -> str:
    parts, h = [], r.hop
    while h is not None:
        parts.append(f"{h.service} {h.version}" if h.version else h.service)
        h = h.next
    return " → ".join(parts)


def trim(s: str) -> str:
    s = " ".join(s.split())
    return s[:160] + "…" if len(s) > 160 else s


def q(s: str) -> str:
    """A string in double quotes, so an empty one shows as \"\"."""
    return json.dumps(s, ensure_ascii=False)


def first_non_empty(*vals: str) -> str:
    return next((v for v in vals if v), "")
