"""Entry point: serve a role, or run the suite on a console (see the package docstring)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request

from . import __version__
from .durations import parse_duration

log = logging.getLogger("platform_probe")


def serve() -> int:
    role = os.environ.get("ROLE") or "console"
    if role == "console":
        from .config import load_config
        from .console import app as console_app
        from .kube import Kube, KubeError
        from .runner import Probe

        cfg = load_config(os.environ.get("CONFIG") or "/etc/platform-probe/config.json")
        try:
            kube = Kube.in_cluster()
        except KubeError as e:
            log.warning("platform checks will fail: %s", e)
            kube = Kube()
        app = console_app(Probe(cfg, kube))
    elif role in ("frontend", "catalog", "pricing", "outsider"):
        from .service import Service, ServiceConfig

        app = Service(
            ServiceConfig(
                role=role,
                version=os.environ.get("VERSION", ""),
                pod=os.environ.get("POD_NAME", ""),
                namespace=os.environ.get("POD_NAMESPACE", ""),
                next=os.environ.get("NEXT", ""),
                next_outlier=os.environ.get("NEXT_OUTLIER", ""),
                faulty=os.environ.get("FAULTY") == "true",
            )
        ).app()
    else:
        log.error("unknown ROLE %r", role)
        return 1
    from aiohttp import web

    port = int(os.environ.get("PORT") or "8080")
    cert, key = os.environ.get("TLS_CERT_FILE"), os.environ.get("TLS_KEY_FILE")
    if cert and key:
        # The ingress suite's backend: HTTP and HTTPS, for edge, reencrypt and passthrough Routes.
        tls_port = int(os.environ.get("TLS_PORT") or "8443")
        log.info("platform-probe %s: %s listening on :%d and TLS :%d", __version__, role, port, tls_port)
        asyncio.run(serve_tls(app, port, tls_port, cert, key))
        return 0
    log.info("platform-probe %s: %s listening on :%d", __version__, role, port)
    web.run_app(app, port=port, print=None, access_log=None, shutdown_timeout=5, handle_signals=True)
    return 0


async def serve_tls(app, port: int, tls_port: int, cert: str, key: str) -> None:
    import signal
    import ssl

    from aiohttp import web

    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, port=port).start()
    await web.TCPSite(runner, port=tls_port, ssl_context=ctx).start()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()


LABEL = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}


def check(argv: list[str]) -> int:
    """Ask a console to run the suite, print the results, and return the exit code."""
    ap = argparse.ArgumentParser(prog="platform-probe check")
    ap.add_argument("--url", default=os.environ.get("CONSOLE_URL") or "http://localhost:8080", help="console base URL")
    ap.add_argument("--junit", default="", help="also write JUnit XML to this file")
    ap.add_argument("--timeout", default="10m", type=parse_duration, help="give up after this long")
    args = ap.parse_args(argv)

    req = urllib.request.Request(args.url.rstrip("/") + "/api/run?wait=true", data=b"", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"run: console answered {e.code} {e.reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"run: {e}", file=sys.stderr)
        return 2

    from .runner import Report, Status

    rep = Report.from_dict(data)
    group = ""
    for r in rep.results:
        if r.group != group:
            group = r.group
            print(f"\n{group}")
        print(f"  {LABEL[r.status]:<4}  {r.name}: {r.detail}")
        if r.status == Status.FAIL and r.hint:
            print(f"        {r.hint}")
    print(f"\n{rep.passed} passed, {rep.failed} failed, {rep.skipped} skipped in {rep.duration_ms / 1000:.0f}s")
    if args.junit:
        from .junit import junit

        try:
            with open(args.junit, "wb") as f:
                f.write(junit(rep))
        except OSError as e:
            print(f"junit: {e}", file=sys.stderr)
    return 1 if rep.failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if argv[:1] == ["check"]:
        return check(argv[1:])
    if argv[:1] == ["task"]:
        from .tasks import main as task

        return task(argv[1:])
    if argv[:1] in (["version"], ["--version"]):
        print(__version__)
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
