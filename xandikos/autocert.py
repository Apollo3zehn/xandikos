# Xandikos
# Copyright (C) 2026 Jelmer Vernooĳ <jelmer@jelmer.uk>, et al.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License or (at your option) any later version of
# the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.

"""Self-signed certificate generation for development/testing use only."""

import datetime
import logging
import os
import ssl

DEFAULT_CERT_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "xandikos",
    "certs",
)

CERT_FILENAME = "selfsigned.crt"
KEY_FILENAME = "selfsigned.key"

# Regenerate when the certificate is within this many days of expiry.
RENEWAL_THRESHOLD_DAYS = 30
# Lifetime for newly issued certificates.
CERT_LIFETIME_DAYS = 365

logger = logging.getLogger(__name__)


def _generate(cert_path: str, key_path: str, hostname: str) -> None:
    """Generate a fresh self-signed certificate and private key."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for --autocert. "
            "Install it with: pip install cryptography"
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=CERT_LIFETIME_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    os.makedirs(os.path.dirname(cert_path), mode=0o700, exist_ok=True)

    # Write the key first with restrictive permissions.
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Write atomically with the right mode from the start.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key_bytes)
    finally:
        os.close(fd)

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _needs_regeneration(cert_path: str) -> bool:
    """Return True if no usable cert exists or the existing one is near expiry."""
    if not os.path.exists(cert_path):
        return True
    try:
        from cryptography import x509
    except ImportError:
        # If cryptography isn't available we can't introspect; assume usable.
        return False
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
    except (OSError, ValueError):
        return True
    expiry = cert.not_valid_after_utc
    threshold = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=RENEWAL_THRESHOLD_DAYS
    )
    return expiry < threshold


def ensure_self_signed(
    cert_dir: str | None = None, hostname: str = "localhost"
) -> tuple[str, str]:
    """Ensure a self-signed certificate exists, generating one if needed.

    Returns:
        Tuple of (cert_path, key_path).
    """
    directory = cert_dir or DEFAULT_CERT_DIR
    cert_path = os.path.join(directory, CERT_FILENAME)
    key_path = os.path.join(directory, KEY_FILENAME)

    if _needs_regeneration(cert_path) or not os.path.exists(key_path):
        logger.info("Generating self-signed certificate at %s", cert_path)
        _generate(cert_path, key_path, hostname)
    else:
        logger.info("Reusing self-signed certificate at %s", cert_path)

    return cert_path, key_path


def make_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Build an SSL context for serving HTTPS from a cert/key pair."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return context
