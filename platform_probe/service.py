"""The demo app the probe sends traffic through: frontend → catalog → pricing.

Every hop answers with what it saw (its version, the caller's mesh identity, the attempt count,
the request ID) and the answer of the hop after it, so a check can see the whole path.

Requests can ask a hop to misbehave, which is how the probe exercises retries, timeouts and
outlier detection:

    x-probe-fail: pricing=0.3       pricing answers 500 on 30% of requests
    x-probe-delay: frontend=3s      frontend waits 3s before answering
    x-probe-scenario: outlier       catalog calls the pricing pool that includes a faulty pod
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import json
import random
import socket
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from .durations import parse_duration


@dataclass
class Hop:
    """One service's view of a request."""

    service: str = ""
    version: str = ""
    pod: str = ""
    namespace: str = ""
    method: str = ""
    path: str = ""
    host: str = ""
    caller: str = ""
    attempt: int = 0
    request_id: str = ""
    status: int = 0
    error: str = ""
    elapsed_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    next: Hop | None = None

    def to_dict(self) -> dict:
        d: dict = {"service": self.service}
        for key, value in (
            ("version", self.version),
            ("pod", self.pod),
            ("namespace", self.namespace),
            ("method", self.method),
            ("path", self.path),
            ("host", self.host),
            ("caller", self.caller),
            ("attempt", self.attempt),
            ("requestId", self.request_id),
        ):
            if value:
                d[key] = value
        d["status"] = self.status
        if self.error:
            d["error"] = self.error
        d["elapsedMs"] = self.elapsed_ms
        if self.headers:
            d["headers"] = self.headers
        if self.next is not None:
            d["next"] = self.next.to_dict()
        return d

    @classmethod
    def from_dict(cls, d) -> Hop | None:
        """A hop from one of the probe's services, or None for anything else."""
        if not isinstance(d, dict) or not isinstance(d.get("service"), str) or not d["service"]:
            return None

        def s(key: str) -> str:
            v = d.get(key)
            return v if isinstance(v, str) else ""

        def i(key: str) -> int:
            v = d.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

        headers = d.get("headers")
        return cls(
            service=d["service"],
            version=s("version"),
            pod=s("pod"),
            namespace=s("namespace"),
            method=s("method"),
            path=s("path"),
            host=s("host"),
            caller=s("caller"),
            attempt=i("attempt"),
            request_id=s("requestId"),
            status=i("status"),
            error=s("error"),
            elapsed_ms=i("elapsedMs"),
            headers={k: v for k, v in headers.items() if isinstance(v, str)} if isinstance(headers, dict) else {},
            next=cls.from_dict(d.get("next")),
        )


@dataclass
class FetchResult:
    """What the outsider saw when it called a URL."""

    url: str = ""
    status: int = 0
    error_kind: str = ""  # timeout, reset, refused, dns or other
    error: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        d: dict = {"url": self.url}
        if self.status:
            d["status"] = self.status
        if self.error_kind:
            d["errorKind"] = self.error_kind
        if self.error:
            d["error"] = self.error
        d["elapsedMs"] = self.elapsed_ms
        return d

    @classmethod
    def from_dict(cls, d: dict) -> FetchResult:
        return cls(
            url=str(d.get("url", "")),
            status=int(d.get("status") or 0),
            error_kind=str(d.get("errorKind", "")),
            error=str(d.get("error", "")),
            elapsed_ms=int(d.get("elapsedMs") or 0),
        )


@dataclass
class ServiceConfig:
    """One instance's role and wiring, from its environment."""

    role: str  # frontend, catalog, pricing or outsider
    version: str = ""
    pod: str = ""
    namespace: str = ""
    # The base URL of the hop after this one; empty for the last hop.
    next: str = ""
    # Replaces next for the outlier scenario (the pool with the faulty pod).
    next_outlier: str = ""
    # Faulty instances answer 500 to every request, for outlier detection to eject.
    faulty: bool = False


# Headers passed on to the next hop: tracing, the version selector and the probe's own controls.
PROPAGATED = (
    "x-request-id",
    "traceparent",
    "tracestate",
    "b3",
    "x-b3-traceid",
    "x-b3-spanid",
    "x-b3-parentspanid",
    "x-b3-sampled",
    "x-b3-flags",
    "x-version",
)


class Service:
    def __init__(self, cfg: ServiceConfig, rand: Callable[[], float] = random.random):
        self.cfg = cfg
        self.rand = rand
        self.seen = Ring(4096)
        self._client: aiohttp.ClientSession | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", healthz)
        if self.cfg.role == "outsider":
            app.router.add_get("/fetch", self.fetch)
            return app
        app.router.add_get("/seen", self.seen_handler)
        app.router.add_route("*", "/{tail:.*}", self.serve)
        app.on_cleanup.append(self._close)
        return app

    def client(self) -> aiohttp.ClientSession:
        if self._client is None:
            # No client timeout: the mesh's own timeouts are what the probe tests, so they must fire first.
            self._client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None),
                connector=aiohttp.TCPConnector(limit_per_host=16, keepalive_timeout=30),
            )
        return self._client

    async def _close(self, _app=None) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def seen_handler(self, request: web.Request) -> web.Response:
        return json_response(200, {"seen": self.seen.has(request.query.get("id", ""))})

    async def serve(self, request: web.Request) -> web.Response:
        start = time.monotonic()
        if probe_id := request.headers.get("x-probe-id"):
            self.seen.add(probe_id)
        hop = Hop(
            service=self.cfg.role,
            version=self.cfg.version,
            pod=self.cfg.pod,
            namespace=self.cfg.namespace,
            method=request.method,
            path=request.path,
            host=request.host,
            caller=caller_from_xfcc(request.headers.get("x-forwarded-client-cert", "")),
            request_id=request.headers.get("x-request-id", ""),
            status=200,
        )
        with contextlib.suppress(ValueError):
            hop.attempt = int(request.headers.get("x-envoy-attempt-count", ""))

        def finish() -> web.Response:
            hop.elapsed_ms = int((time.monotonic() - start) * 1000)
            resp = json_response(hop.status, hop.to_dict())
            resp.headers["x-probe-service"] = self.cfg.role
            resp.headers["x-probe-version"] = self.cfg.version
            return resp

        delay = directive(request.headers.get("x-probe-delay", ""), self.cfg.role)
        if delay is not None:
            with contextlib.suppress(ValueError):
                await asyncio.sleep(parse_duration(delay))
        if self.cfg.faulty:
            hop.status, hop.error = 500, "faulty instance"
            return finish()
        fail = directive(request.headers.get("x-probe-fail", ""), self.cfg.role)
        if fail is not None:
            try:
                if self.rand() < float(fail):
                    hop.status, hop.error = 500, "injected failure"
                    return finish()
            except ValueError:
                pass
        # /echo shows the request as this hop received it, for rewrite and header checks.
        if request.path.startswith("/echo"):
            hop.headers = flatten(request.headers)
            return finish()

        nxt = self.cfg.next
        if request.headers.get("x-probe-scenario") == "outlier" and self.cfg.next_outlier:
            nxt = self.cfg.next_outlier
        if nxt:
            hop.next = await self.call(request, nxt)
            # 502, not the upstream's own code: the mesh retries 503 by default, which would hide
            # the failure the probe is trying to observe one hop further down.
            if hop.next.status >= 400 or hop.next.status == 0:
                hop.status = 502
        return finish()

    async def call(self, incoming: web.Request, base: str) -> Hop:
        """Forward the request to the next hop and return that hop's view, or what went wrong."""
        start = time.monotonic()
        headers: CIMultiDict[str] = CIMultiDict()
        for h in PROPAGATED:
            if v := incoming.headers.get(h):
                headers[h] = v
        for k, v in incoming.headers.items():
            if k.lower().startswith("x-probe-"):
                headers.add(k, v)
        try:
            async with self.client().request(incoming.method, base.rstrip("/") + "/", headers=headers) as resp:
                body = await resp.content.read(1 << 20)
                status = resp.status
        except Exception as e:  # noqa: BLE001 - every failure is reported in the hop
            return Hop(service=base, error=describe_error(e), elapsed_ms=int((time.monotonic() - start) * 1000))
        try:
            hop = Hop.from_dict(json.loads(body))
        except ValueError:
            hop = None
        if hop is None:
            # Not one of ours: the sidecar answered (fault abort, RBAC denial, timeout).
            hop = Hop(service=base, error=body.decode("utf-8", "replace").strip())
        hop.status = status
        if hop.elapsed_ms == 0:
            hop.elapsed_ms = int((time.monotonic() - start) * 1000)
        return hop

    async def fetch(self, request: web.Request) -> web.Response:
        """Let the probe call a service from a pod outside the mesh, to see what NetworkPolicies
        and STRICT mTLS do to traffic that doesn't come from a sidecar. Only in-cluster targets."""
        target = request.query.get("url", "")
        try:
            u = urlsplit(target)
            host = u.hostname or ""
        except ValueError:
            u, host = None, ""
        if u is None or u.scheme != "http" or not in_cluster(host):
            return json_response(400, {"error": "url must be http:// to a cluster Service"})
        res = FetchResult(url=target)
        start = time.monotonic()
        try:
            async with (
                asyncio.timeout(4),
                aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s,
                s.get(target) as resp,
            ):
                res.status = resp.status
        except Exception as e:  # noqa: BLE001 - the error is the result
            res.error_kind, res.error = classify_error(e), describe_error(e)
        res.elapsed_ms = int((time.monotonic() - start) * 1000)
        return json_response(200, res.to_dict())


async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


def json_response(status: int, body) -> web.Response:
    return web.Response(status=status, text=json.dumps(body) + "\n", content_type="application/json")


def in_cluster(host: str) -> bool:
    """Accept a Service name, <svc>.<ns>, or a name under .svc / .svc.cluster.local."""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if host.count(".") <= 1:
        return True
    return host.endswith(".svc") or host.endswith(".svc.cluster.local")


def describe_error(e: BaseException) -> str:
    """str(e), or the exception's type when that is empty (as for a timeout)."""
    text = str(e).strip()
    return text if text else type(e).__name__


def _errnos(e: BaseException):
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, OSError) and e.errno is not None:
            yield e.errno
        os_error = getattr(e, "os_error", None)
        if isinstance(os_error, OSError) and os_error.errno is not None:
            yield os_error.errno
        e = e.__cause__ or e.__context__


def classify_error(e: BaseException) -> str:
    """Tell a NetworkPolicy drop (timeout) from an mTLS rejection (reset) and the rest."""
    if isinstance(e, (TimeoutError, asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
        return "timeout"
    if isinstance(e, (aiohttp.ClientConnectorDNSError, socket.gaierror)):
        return "dns"
    codes = set(_errnos(e))
    if codes & {errno.ECONNRESET, errno.EPIPE, errno.ECONNABORTED}:
        return "reset"
    if errno.ECONNREFUSED in codes:
        return "refused"
    if errno.ETIMEDOUT in codes:
        return "timeout"
    if isinstance(e, (aiohttp.ServerDisconnectedError, ConnectionResetError, BrokenPipeError)):
        return "reset"
    msg = str(e).lower()
    if any(s in msg for s in ("connection reset", "eof", "broken pipe", "disconnected")):
        return "reset"
    if "connection refused" in msg:
        return "refused"
    if any(s in msg for s in ("no such host", "name or service not known", "nodename nor servname")):
        return "dns"
    return "other"


def directive(header: str, role: str) -> str | None:
    """Read "role=value" pairs ("pricing=0.3, catalog=1") and return the value for role."""
    for part in header.split(","):
        k, sep, v = part.strip().partition("=")
        if sep and k.strip() == role:
            return v.strip()
    return None


def caller_from_xfcc(xfcc: str) -> str:
    """The SPIFFE ID of the immediate caller from an X-Forwarded-Client-Cert header: the URI
    field of its last element, which the receiving sidecar appended."""
    if not xfcc:
        return ""
    elements = split_outside_quotes(xfcc, ",")
    for fld in split_outside_quotes(elements[-1], ";"):
        k, sep, v = fld.strip().partition("=")
        if sep and k.lower() == "uri":
            return v.strip('"')
    return ""


def split_outside_quotes(s: str, sep: str) -> list[str]:
    out, cur = [], []
    quoted = escaped = False
    for c in s:
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == '"':
            quoted = not quoted
        elif c == sep and not quoted:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    out.append("".join(cur))
    return out


def flatten(headers) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    for k, v in headers.items():
        out.setdefault(k.lower(), []).append(v)
    return {k: ", ".join(v) for k, v in out.items()}


class Ring:
    """Remembers the last n probe request IDs, so a check can ask whether a mirrored copy arrived."""

    def __init__(self, n: int):
        self.n = n
        self.ids: OrderedDict[str, None] = OrderedDict()

    def add(self, probe_id: str) -> None:
        if probe_id in self.ids:
            return
        self.ids[probe_id] = None
        if len(self.ids) > self.n:
            self.ids.popitem(last=False)

    def has(self, probe_id: str) -> bool:
        return probe_id != "" and probe_id in self.ids
