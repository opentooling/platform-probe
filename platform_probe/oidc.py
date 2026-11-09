"""OIDC access tokens for the gateway's token check, with the client credentials grant."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import quote_plus

import aiohttp

from .runner import Probe, ProbeError, do


async def oidc_token(p: Probe) -> str:
    """Get an access token with the client credentials grant, and reuse it until shortly before
    it expires."""
    if p.token_value and time.monotonic() < p.token_expires:
        return p.token_value
    o = p.cfg.oidc
    try:
        with open(o.client_secret_file, encoding="utf-8") as f:
            secret = f.read().strip()
    except OSError as e:
        raise ProbeError(f"client secret: {e}") from None

    disc = await do(p.mesh, "GET", o.issuer_url.rstrip("/") + "/.well-known/openid-configuration")
    if disc.err is not None or disc.status != 200:
        raise ProbeError(f"discovery: {disc}")
    try:
        endpoint = json.loads(disc.body).get("token_endpoint", "")
    except (ValueError, AttributeError):
        endpoint = ""
    if not endpoint:
        raise ProbeError("discovery document has no token_endpoint")

    form = {"grant_type": "client_credentials"}
    if o.scope:
        form["scope"] = o.scope
    # RFC 6749 2.3.1: client ID and secret are form-encoded before they go into Basic auth.
    auth = aiohttp.BasicAuth(quote_plus(o.client_id), quote_plus(secret))
    async with p.mesh.post(endpoint, data=form, auth=auth) as resp:
        try:
            tok = await resp.json(content_type=None)
        except ValueError:
            tok = {}
        status = resp.status
    if not isinstance(tok, dict):
        tok = {}
    access = tok.get("access_token") or ""
    if status != 200 or not access:
        raise ProbeError(f"token endpoint answered {status} {tok.get('error_description', '')}".rstrip())
    ttl = tok.get("expires_in") or 60
    p.token_value = access
    p.token_expires = time.monotonic() + float(ttl) - 15
    return access


def tamper(token: str) -> str:
    """Flip the first byte of the signature, so the token no longer verifies.

    It works on the decoded bytes: the last base64url character of a signature can carry
    padding bits that decoders ignore, so changing a character can leave the signature intact
    (for RS256, a quarter of the time).
    """
    parts = token.split(".")
    if len(parts) != 3:
        return token + "x"
    try:
        sig = bytearray(base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)))
    except ValueError:
        return token + "x"
    if not sig:
        return token + "x"
    sig[0] ^= 0xFF
    parts[2] = base64.urlsafe_b64encode(bytes(sig)).rstrip(b"=").decode()
    return ".".join(parts)
