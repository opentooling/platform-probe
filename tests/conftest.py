"""Shared fixtures: a self-signed certificate for a fake gateway, and a probe pointed at it."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

GATEWAY_HOST = "probe.api.example.internal"


def run(coro):
    """Run a coroutine to completion. The tests need no async plugin."""
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def server_cert(tmp_path_factory) -> tuple[Path, Path]:
    """A certificate for the gateway hostname, issued by "probe-test-ca", valid for 90 days."""
    d = tmp_path_factory.mktemp("cert")
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, GATEWAY_HOST)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "probe-test-ca")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(GATEWAY_HOST), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = d / "tls.crt", d / "tls.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_file, key_file


@pytest.fixture
def server_ssl(server_cert) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(*server_cert)
    return ctx
