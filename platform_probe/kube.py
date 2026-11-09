"""A minimal read-only Kubernetes client: GET a path with the pod's own ServiceAccount token,
decode the JSON. The probe only reads object status, so it needs nothing more than that, and
no client library."""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass

import aiohttp

from .service import read_body

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeError(Exception):
    pass


class StatusError(KubeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Kube:
    """A client for the API server. Without a cluster (base is None), every call fails with
    "no Kubernetes API access", so the platform checks fail with that rather than crash."""

    def __init__(self, base: str | None = None, token_file: str = "", ca_file: str = ""):
        self.base = base
        self.token_file = token_file
        self.ca_file = ca_file
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def in_cluster(cls) -> Kube:
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT")
        if not host:
            raise KubeError("not running in a cluster (KUBERNETES_SERVICE_HOST is unset)")
        if ":" in host:
            host = f"[{host}]"
        ca_file = os.path.join(SA_DIR, "ca.crt")
        if not os.path.exists(ca_file):
            raise KubeError(f"{ca_file} not found")
        return cls(f"https://{host}:{port}", os.path.join(SA_DIR, "token"), ca_file)

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            ctx = ssl.create_default_context(cafile=self.ca_file or None)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10), connector=aiohttp.TCPConnector(ssl=ctx)
            )
        return self._session

    async def get(self, path: str) -> dict:
        """Fetch an API path such as /apis/gateway.networking.k8s.io/v1/namespaces/x/gateways/y."""
        if self.base is None:
            raise KubeError("no Kubernetes API access")
        # Re-read every time: projected tokens rotate.
        with open(self.token_file, encoding="utf-8") as f:
            token = f.read().strip()
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        async with self._client().get(self.base + path, headers=headers) as resp:
            body = await read_body(resp, 8 << 20)
            try:
                obj = json.loads(body)
            except ValueError:
                obj = None
            if resp.status != 200:
                message = obj.get("message", "") if isinstance(obj, dict) else ""
                raise StatusError(resp.status, message)
            if not isinstance(obj, dict):
                raise KubeError(f"{path}: not a JSON object ({len(body)} bytes)")
            return obj

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def is_not_found(err: BaseException) -> bool:
    """True for a 404, which for a CRD path also means the API isn't installed."""
    return isinstance(err, StatusError) and err.code == 404


def items(lst: dict) -> list[dict]:
    """The items of a list."""
    raw = lst.get("items")
    return [it for it in raw if isinstance(it, dict)] if isinstance(raw, list) else []


def _dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def name(obj: dict) -> str:
    """metadata.name"""
    n = _dig(obj, "metadata", "name")
    return n if isinstance(n, str) else ""


def labels(obj: dict) -> dict[str, str]:
    raw = _dig(obj, "metadata", "labels")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str)}


@dataclass
class Condition:
    type: str = ""
    status: str = ""
    reason: str = ""
    message: str = ""


def conditions(raw) -> list[Condition]:
    """Read a conditions list."""
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        m = it if isinstance(it, dict) else {}
        out.append(
            Condition(*(m.get(k) if isinstance(m.get(k), str) else "" for k in ("type", "status", "reason", "message")))
        )
    return out


def status_conditions(obj: dict) -> list[Condition]:
    """status.conditions of an object."""
    return conditions(_dig(obj, "status", "conditions"))


def find(conds: list[Condition], typ: str) -> Condition:
    """The condition of the given type, or an empty one."""
    for c in conds:
        if c.type == typ:
            return c
    return Condition()
