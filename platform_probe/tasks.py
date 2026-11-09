"""Tasks the platform suites' test pods run: `python -m platform_probe task <name> '<json args>'`.

Each prints its result as JSON and writes it to the termination message, which the console
reads from the pod's status. A task that can't do its job reports {"error": ...} and exits 0,
so the pod's phase says whether it ran and the result says what it found. Standard library
only, apart from cryptography for reading certificates.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
from datetime import UTC, datetime
from urllib.parse import urlsplit

TERMINATION_LOG = "/dev/termination-log"
CA_FILE = "/etc/platform-probe/ca/ca-bundle.crt"


def _cert_info(der: bytes | None) -> dict:
    if not der:
        return {}
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    cert = x509.load_der_x509_certificate(der)

    def cn(name) -> str:
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return str(attrs[0].value) if attrs else ""

    days = int((cert.not_valid_after_utc - datetime.now(UTC)).total_seconds() / 86400)
    return {
        "issuer": cert.issuer.rfc4514_string(),
        "issuerCN": cn(cert.issuer),
        "subjectCN": cn(cert.subject),
        "notAfter": cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days,
    }


def _fetch_one(url: str, verify: bool = True, timeout: float = 10.0) -> dict:
    u = urlsplit(url)
    out: dict = {"url": url}
    try:
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            if os.path.exists(CA_FILE) and os.path.getsize(CA_FILE):
                ctx.load_verify_locations(CA_FILE)
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                u.hostname, u.port or 443, context=ctx, timeout=timeout
            )
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        conn.request("GET", u.path or "/")
        resp = conn.getresponse()
        body = resp.read(65536)
        out["status"] = resp.status
        if loc := resp.getheader("Location"):
            out["location"] = loc
        if u.scheme == "https":
            out["cert"] = _cert_info(conn.sock.getpeercert(binary_form=True))
        try:
            data = json.loads(body)
            if isinstance(data, dict) and isinstance(data.get("service"), str):
                out["service"] = data["service"]
        except ValueError:
            out["body"] = body[:120].decode("utf-8", "replace")
        conn.close()
    except ssl.SSLCertVerificationError as e:
        out["error"], out["errorKind"] = f"certificate not trusted: {e.verify_message}", "untrusted"
    except (OSError, http.client.HTTPException) as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["errorKind"] = "timeout" if isinstance(e, TimeoutError) else "network"
    return out


def fetch(requests: list[dict]) -> dict:
    """GET each {url, verify} and report status, redirect, certificate and which service answered."""
    return {"results": [_fetch_one(**r) for r in requests]}


def write(path: str, data: str) -> dict:
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return {"written": len(data)}


def read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return {"data": f.read()}


def whoami() -> dict:
    return {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()}


MARKER = "platform-probe marker run="
METRIC = "platform_probe_marker"


def _metrics_handler(run: str):
    from http.server import BaseHTTPRequestHandler

    body = f'# TYPE {METRIC} gauge\n{METRIC}{{run="{run}"}} 1\n'.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path.split("?")[0] in ("/metrics", "/") else 404)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def emit(run: str, seconds: float = 600, metrics_port: int = 9090, interval: float = 5) -> dict:
    """Print the run's marker line every few seconds and serve it as a metric, for the log
    collector to ship and the metrics collector to scrape, until stopped or seconds pass."""
    import threading
    import time
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("", metrics_port), _metrics_handler(run))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lines, end = 0, time.monotonic() + seconds
    while time.monotonic() < end:
        print(MARKER + run, flush=True)
        lines += 1
        time.sleep(interval)
    server.shutdown()
    return {"lines": lines}


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _otlp_post(endpoint: str, signal: str, payload: dict, timeout: float) -> dict:
    import urllib.error
    import urllib.request

    url = endpoint.rstrip("/") + f"/v1/{signal}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "error": f"{e.code} {e.read(300).decode('utf-8', 'replace')}"}
    except OSError as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}


def otlp(
    endpoint: str,
    run: str,
    trace_id: str = "",
    metric: bool = False,
    namespace: str = "",
    pod: str = "",
    timeout: float = 10,
) -> dict:
    """Send a span with the given trace ID and/or the marker metric to an OTLP/HTTP endpoint
    (JSON encoding), as an instrumented app in a tenant namespace would."""
    import secrets
    import time

    now = time.time_ns()
    resource = {
        "attributes": [
            _attr("service.name", "platform-probe"),
            _attr("k8s.namespace.name", namespace),
            _attr("k8s.pod.name", pod or os.environ.get("HOSTNAME", "")),
        ]
    }
    scope = {"name": "platform-probe"}
    out: dict = {}
    if trace_id:
        span = {
            "traceId": trace_id,
            "spanId": secrets.token_hex(8),
            "name": "platform-probe marker",
            "kind": 1,
            "startTimeUnixNano": str(now - 1_000_000),
            "endTimeUnixNano": str(now),
            "attributes": [_attr("platform_probe.run", run)],
        }
        payload = {"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": scope, "spans": [span]}]}]}
        out["traces"] = _otlp_post(endpoint, "traces", payload, timeout)
    if metric:
        point = {"asInt": "1", "timeUnixNano": str(now), "attributes": [_attr("run", run)]}
        m = {"name": METRIC, "gauge": {"dataPoints": [point]}}
        payload = {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": scope, "metrics": [m]}]}]}
        out["metrics"] = _otlp_post(endpoint, "metrics", payload, timeout)
    errors = [f"{k}: {v['error']}" for k, v in out.items() if v.get("error")]
    if errors:
        out["error"] = "; ".join(errors)
    return out


TASKS = {"fetch": fetch, "write": write, "read": read, "whoami": whoami, "emit": emit, "otlp": otlp}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in TASKS:
        print(f"usage: task {{{','.join(TASKS)}}} '<json args>'", file=sys.stderr)
        return 2
    try:
        args = json.loads(argv[1]) if len(argv) > 1 else {}
        result = TASKS[argv[0]](**args)
    except Exception as e:  # noqa: BLE001 - every failure is the result
        result = {"error": f"{type(e).__name__}: {e}"}
    text = json.dumps(result)
    print(text)
    try:
        with open(os.environ.get("TERMINATION_LOG", TERMINATION_LOG), "w", encoding="utf-8") as f:
            f.write(text[:4000])
    except OSError:
        pass
    return 0
