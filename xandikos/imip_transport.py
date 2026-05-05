# Xandikos
# Copyright (C) 2026 Jelmer Vernooij <jelmer@jelmer.uk>, et al.
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

"""Outbound iMIP transports.

These transports take a fully-formed :class:`email.message.EmailMessage`
(typically built by :func:`xandikos.imip.build_message`) and hand it off to
some delivery mechanism. Sending is synchronous; transport failures raise
:class:`IMIPTransportError` so callers can record SCHEDULE-STATUS=5.1
without aborting the originating CalDAV operation.
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import subprocess
from email.message import EmailMessage
from typing import Protocol


logger = logging.getLogger(__name__)


class IMIPTransportError(Exception):
    """Raised when an outbound iMIP message could not be handed off."""


class IMIPTransport(Protocol):
    """Synchronous outbound transport for iMIP email."""

    def send(self, message: EmailMessage) -> None:
        """Deliver *message*.

        Raises:
          IMIPTransportError: if the message could not be handed to the
            underlying transport.
        """


class NullTransport:
    """Transport that silently drops every message.

    Intended as the default when outbound iMIP is disabled.
    """

    def send(self, message: EmailMessage) -> None:
        del message


class CapturingTransport:
    """In-memory transport that records every message it is asked to send.

    Tests use this to assert that implicit scheduling produced the expected
    iMIP messages.
    """

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class SendmailTransport:
    """Pipe outbound iMIP through ``sendmail -t -i``.

    The recipient list comes from the message's ``To``/``Cc``/``Bcc`` headers
    (``-t``), and lone dots in the body are not treated as end-of-input
    (``-i``). This is the lowest-config option for hosts that already have a
    working MTA.
    """

    def __init__(
        self,
        binary: str = "/usr/sbin/sendmail",
        *,
        timeout: float = 30.0,
    ) -> None:
        self.binary = binary
        self.timeout = timeout

    def send(self, message: EmailMessage) -> None:
        try:
            completed = subprocess.run(
                [self.binary, "-t", "-i"],
                input=bytes(message),
                timeout=self.timeout,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise IMIPTransportError(
                f"sendmail binary not found: {self.binary}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise IMIPTransportError(
                f"sendmail did not exit within {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise IMIPTransportError(f"sendmail invocation failed: {exc}") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", "replace").strip()
            raise IMIPTransportError(
                f"sendmail exited {completed.returncode}: {stderr or '(no stderr)'}"
            )


class SMTPTransport:
    """Deliver outbound iMIP via SMTP.

    Supports plain SMTP, implicit TLS (``use_ssl=True``), and STARTTLS
    upgrade (``use_starttls=True``). If *username* is given, ``LOGIN``
    authentication is performed after any TLS handshake.
    """

    def __init__(
        self,
        host: str,
        port: int = 25,
        *,
        username: str | None = None,
        password: str | None = None,
        use_starttls: bool = False,
        use_ssl: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if use_starttls and use_ssl:
            raise ValueError("use_starttls and use_ssl are mutually exclusive")
        if username is not None and password is None:
            raise ValueError("password is required when username is set")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_starttls = use_starttls
        self.use_ssl = use_ssl
        self.timeout = timeout

    def send(self, message: EmailMessage) -> None:
        try:
            client = self._connect()
        except (OSError, smtplib.SMTPException) as exc:
            raise IMIPTransportError(
                f"could not connect to {self.host}:{self.port}: {exc}"
            ) from exc
        try:
            if self.use_starttls:
                client.starttls()
                client.ehlo()
            if self.username is not None:
                assert self.password is not None
                client.login(self.username, self.password)
            client.send_message(message)
        except smtplib.SMTPException as exc:
            raise IMIPTransportError(f"SMTP delivery failed: {exc}") from exc
        finally:
            try:
                client.quit()
            except smtplib.SMTPException:
                pass

    def _connect(self) -> smtplib.SMTP:
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register outbound iMIP CLI flags on *parser*.

    Each flag has a matching ``XANDIKOS_*`` environment variable so Docker
    deployments can configure SMTP without rewriting the entrypoint.
    """
    group = parser.add_argument_group(title="Outbound iMIP (RFC 6047)")
    group.add_argument(
        "--imip-send",
        choices=["off", "sendmail", "smtp"],
        default=_env("XANDIKOS_IMIP_SEND", "off"),
        help=(
            "Outbound iMIP delivery mode for non-local attendees. "
            "[default: off; env: XANDIKOS_IMIP_SEND]"
        ),
    )
    group.add_argument(
        "--smtp-from",
        default=_env("XANDIKOS_SMTP_FROM"),
        help=(
            "Sender address used in the From: header of outbound iMIP "
            "(server identity; the originating organiser/attendee goes in "
            "Reply-To). [env: XANDIKOS_SMTP_FROM]"
        ),
    )
    group.add_argument(
        "--sendmail-binary",
        default=_env("XANDIKOS_SENDMAIL_BINARY", "/usr/sbin/sendmail"),
        help=(
            "Path to the sendmail-compatible binary used when "
            "--imip-send=sendmail. [default: %(default)s; "
            "env: XANDIKOS_SENDMAIL_BINARY]"
        ),
    )
    group.add_argument(
        "--smtp-host",
        default=_env("XANDIKOS_SMTP_HOST"),
        help="SMTP relay hostname. [env: XANDIKOS_SMTP_HOST]",
    )
    group.add_argument(
        "--smtp-port",
        type=int,
        default=int(_env("XANDIKOS_SMTP_PORT", "25") or "25"),
        help="SMTP relay port. [default: %(default)s; env: XANDIKOS_SMTP_PORT]",
    )
    group.add_argument(
        "--smtp-encryption",
        choices=["none", "starttls", "ssl"],
        default=_env("XANDIKOS_SMTP_ENCRYPTION", "none"),
        help=(
            "Transport encryption for SMTP. [default: %(default)s; "
            "env: XANDIKOS_SMTP_ENCRYPTION]"
        ),
    )
    group.add_argument(
        "--smtp-user",
        default=_env("XANDIKOS_SMTP_USER"),
        help="SMTP LOGIN username. [env: XANDIKOS_SMTP_USER]",
    )
    group.add_argument(
        "--smtp-password-file",
        default=_env("XANDIKOS_SMTP_PASSWORD_FILE"),
        help=(
            "Path to a file containing the SMTP LOGIN password. "
            "[env: XANDIKOS_SMTP_PASSWORD_FILE]"
        ),
    )


def from_args(args: argparse.Namespace) -> IMIPTransport:
    """Build an :class:`IMIPTransport` from parsed CLI arguments.

    Returns :class:`NullTransport` when ``--imip-send=off`` (the default).
    Raises :class:`SystemExit` via ``argparse.ArgumentTypeError`` semantics
    when a chosen mode is missing required configuration.
    """
    mode = getattr(args, "imip_send", None) or "off"
    if mode == "off":
        return NullTransport()
    if mode == "sendmail":
        return SendmailTransport(binary=args.sendmail_binary)
    if mode == "smtp":
        if not args.smtp_host:
            raise argparse.ArgumentTypeError(
                "--imip-send=smtp requires --smtp-host (or XANDIKOS_SMTP_HOST)"
            )
        password: str | None = None
        if args.smtp_user is not None:
            if not args.smtp_password_file:
                raise argparse.ArgumentTypeError(
                    "--smtp-user requires --smtp-password-file "
                    "(or XANDIKOS_SMTP_PASSWORD_FILE)"
                )
            with open(args.smtp_password_file) as f:
                password = f.read().rstrip("\n")
        encryption = args.smtp_encryption or "none"
        return SMTPTransport(
            args.smtp_host,
            args.smtp_port,
            username=args.smtp_user,
            password=password,
            use_starttls=(encryption == "starttls"),
            use_ssl=(encryption == "ssl"),
        )
    raise argparse.ArgumentTypeError(f"unknown --imip-send mode: {mode!r}")
