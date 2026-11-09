"""What the chart tells the console about the platform and the probe's own resources.

The chart renders it into a ConfigMap as JSON (templates/config.yaml), so the chart's values are
the only source of truth. Unknown keys are an error: the chart renders this file, so one means
the two have drifted.
"""

from __future__ import annotations

import dataclasses
import json
import os
import typing
from dataclasses import dataclass, field

from .durations import format_duration, parse_duration


class ConfigError(ValueError):
    pass


def _json(name: str, **kw):
    """A field whose JSON key isn't the camelCase of its Python name."""
    return field(metadata={"json": name}, **kw)


def _duration(**kw):
    """A field read from a "10s"-style string, held as seconds."""
    return field(default=0.0, metadata={"duration": True}, **kw)


@dataclass
class Gateway:
    name: str = ""
    namespace: str = ""
    service_account: str = ""
    # host is the hostname of the probe's routes; address is where the console dials it.
    host: str = ""
    address: str = ""
    http_port: int = 0
    https_port: int = 0
    issuer_contains: str = ""
    insecure_skip_verify: bool = False


@dataclass
class Kuadrant:
    namespace: str = ""


@dataclass
class Services:
    """Base URLs of the probe's Services."""

    frontend: str = ""
    catalog: str = ""
    catalog_v1: str = ""
    catalog_v2: str = ""
    outsider: str = ""


@dataclass
class ServiceAccounts:
    """The ServiceAccounts the probe's pods run as."""

    frontend: str = ""
    catalog: str = ""
    pricing: str = ""
    console: str = ""


@dataclass
class Expect:
    """What the platform is expected to enforce. A check for something that is off is skipped."""

    default_deny: bool = False
    http_redirect: bool = False
    strict_mtls: bool = _json("strictMTLS", default=False)
    authorization_policies: bool = False
    traffic_management: bool = False
    network_policies: bool = False


@dataclass
class Limit:
    count: int = _json("limit", default=0)
    window: float = _duration()

    def __str__(self) -> str:
        return f"{self.count} / {format_duration(self.window)}"

    def to_json(self) -> dict:
        return {"limit": self.count, "window": format_duration(self.window)}


@dataclass
class Limits:
    public: Limit = field(default_factory=Limit)
    free: Limit = field(default_factory=Limit)
    gold: Limit = field(default_factory=Limit)


@dataclass
class Routes:
    split_v2_percent: int = 0
    timeout: float = _duration()


@dataclass
class Mesh:
    split_v2_percent: int = 0


@dataclass
class Egress:
    registry_only: bool = False
    blocked_url: str = ""
    allowed_url: str = ""


@dataclass
class APIKeys:
    prefix: str = ""
    # Directory holding one file per user (alice, bob), each the user's key.
    dir: str = ""


@dataclass
class OIDC:
    enabled: bool = False
    issuer_url: str = ""
    client_id: str = ""
    scope: str = ""
    client_secret_file: str = ""


@dataclass
class Config:
    release: str = ""
    namespace: str = ""
    trust_domain: str = ""
    gateway: Gateway = field(default_factory=Gateway)
    kuadrant: Kuadrant = field(default_factory=Kuadrant)
    services: Services = field(default_factory=Services)
    service_accounts: ServiceAccounts = field(default_factory=ServiceAccounts)
    expect: Expect = field(default_factory=Expect)
    limits: Limits = field(default_factory=Limits)
    routes: Routes = field(default_factory=Routes)
    mesh: Mesh = field(default_factory=Mesh)
    egress: Egress = field(default_factory=Egress)
    api_keys: APIKeys = field(default_factory=APIKeys)
    oidc: OIDC = field(default_factory=OIDC)
    # CA bundle (PEM) trusted in addition to the system roots, for the gateway and the IdP.
    ca_file: str = ""

    def apply_defaults(self) -> Config:
        if not self.trust_domain:
            self.trust_domain = "cluster.local"
        g = self.gateway
        if not g.http_port:
            g.http_port = 80
        if not g.https_port:
            g.https_port = 443
        if not g.service_account:
            g.service_account = g.name + "-istio"
        if not g.address:
            g.address = f"{g.name}-istio.{g.namespace}.svc"
        if not self.api_keys.prefix:
            self.api_keys.prefix = "APIKEY"
        if not self.routes.timeout:
            self.routes.timeout = 2.0
        return self

    def principal(self, namespace: str, sa: str) -> str:
        """The SPIFFE ID of a ServiceAccount, as the mesh reports it."""
        return f"spiffe://{self.trust_domain}/ns/{namespace}/sa/{sa}"

    def api_key(self, user: str) -> str:
        """A user's key, from the mounted Secret."""
        if not self.api_keys.dir:
            return ""
        try:
            with open(os.path.join(self.api_keys.dir, user), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def url(self, path: str) -> str:
        """A URL on the probe's gateway hostname."""
        return "https://" + self.gateway.host + path


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _decode(cls, data, where: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'config'}: expected an object")
    hints = typing.get_type_hints(cls)
    by_key = {f.metadata.get("json", _camel(f.name)): f for f in dataclasses.fields(cls)}
    obj = cls()
    for key, value in data.items():
        path = f"{where}.{key}" if where else key
        f = by_key.get(key)
        if f is None:
            raise ConfigError(f"unknown key {path}")
        typ = hints[f.name]
        if f.metadata.get("duration"):
            if not isinstance(value, str):
                raise ConfigError(f"{path}: expected a duration string such as 10s")
            try:
                value = parse_duration(value) if value else 0.0
            except ValueError as e:
                raise ConfigError(f"{path}: {e}") from None
        elif dataclasses.is_dataclass(typ):
            value = _decode(typ, value, path)
        elif typ is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{path}: expected true or false")
        elif typ is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{path}: expected an integer")
        elif typ is str and not isinstance(value, str):
            raise ConfigError(f"{path}: expected a string")
        setattr(obj, f.name, value)
    return obj


def parse_config(data: dict) -> Config:
    return _decode(Config, data, "").apply_defaults()


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    try:
        return parse_config(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None
