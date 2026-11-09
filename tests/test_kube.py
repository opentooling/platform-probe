import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import run

from platform_probe.kube import Kube, StatusError


def big_list(n: int) -> dict:
    return {
        "kind": "PodList",
        "items": [{"metadata": {"name": f"pod-{i}", "labels": {"a": "b" * 50}}} for i in range(n)],
    }


async def chunked(request: web.Request) -> web.StreamResponse:
    """A large list sent in small chunks with pauses, the way the API server streams one."""
    body = json.dumps(big_list(2000)).encode()
    resp = web.StreamResponse()
    resp.content_type = "application/json"
    await resp.prepare(request)
    for i in range(0, len(body), 4096):
        await resp.write(body[i : i + 4096])
        await asyncio.sleep(0.001)  # let each chunk go out on its own
    await resp.write_eof()
    return resp


async def forbidden(_request: web.Request) -> web.Response:
    return web.json_response({"kind": "Status", "message": "pods is forbidden"}, status=403)


def test_get_reads_a_chunked_body_whole(tmp_path):
    """A list bigger than one network read must parse: reading only the first chunk broke the
    Gateway, HTTPRoute, policy, sidecar and NetworkPolicy checks with "not a JSON object"."""
    token = tmp_path / "token"
    token.write_text("t")

    async def go():
        app = web.Application()
        app.router.add_get("/api/v1/pods", chunked)
        app.router.add_get("/forbidden", forbidden)
        srv = TestServer(app)
        await srv.start_server()
        k = Kube(str(srv.make_url("")).rstrip("/"), str(token))
        try:
            lst = await k.get("/api/v1/pods")
            try:
                await k.get("/forbidden")
                err = None
            except StatusError as e:
                err = e
        finally:
            await k.close()
            await srv.close()
        return lst, err

    lst, err = run(go())
    assert len(lst["items"]) == 2000
    assert err is not None and err.code == 403 and "forbidden" in err.message
