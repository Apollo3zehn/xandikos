iMIP (Email-based Scheduling)
=============================

iMIP (:rfc:`6047`) is the email transport for iTIP scheduling
messages — the REQUEST/REPLY/CANCEL traffic that flows between an
organiser and remote attendees who don't share a CalDAV server.
This page explains how to set up Xandikos as a participant in iMIP
exchanges.

Concepts
--------

A scheduling exchange has two halves:

**Outbound iMIP**
   Xandikos turns local PUT/DELETE actions on calendar events into
   iTIP messages and emails them to remote attendees or organisers.
   This is *off* by default; enable it with ``--imip-send``.

**Inbound iMIP**
   A remote organiser's mail arrives at the user's mailbox. Something
   has to extract the iTIP payload and POST it into the user's
   schedule inbox so Xandikos can apply it. Two transports are
   supported:

   - the ``xandikos import-imip`` subcommand, invoked from a Sieve
     pipe or any other delivery hook;
   - an LMTP listener built into ``xandikos serve`` that an MTA or
     Sieve script can deliver to directly;
   - a separate ``xandikos-milter`` daemon that Postfix or Sendmail
     consults at SMTP intake time.

Outbound iMIP traffic carries an ``Auto-Submitted: auto-generated``
header (:rfc:`3834`); the inbound paths skip such messages so a
shared mail loop between two Xandikos servers cannot run away.

Outbound iMIP
-------------

Pick one transport.

Sendmail
~~~~~~~~

The simplest setup on a host that already has a working
``/usr/sbin/sendmail`` (Postfix, Exim, ssmtp, msmtp, ...):

.. code-block:: bash

   xandikos serve --directory /var/lib/xandikos \
       --imip-send=sendmail \
       --smtp-from "Xandikos <calendar@example.com>"

Xandikos pipes the message to ``sendmail -t -i``. ``--smtp-from``
sets the ``From:`` header (server identity); the originating
organiser/attendee mail address goes in ``Reply-To:`` so replies
reach the right human.

SMTP
~~~~

For Docker or any host without a local sendmail:

.. code-block:: bash

   xandikos serve --directory /var/lib/xandikos \
       --imip-send=smtp \
       --smtp-host=smtp.example.com --smtp-port=587 \
       --smtp-encryption=starttls \
       --smtp-user=calendar@example.com \
       --smtp-password-file=/run/secrets/xandikos-smtp \
       --smtp-from "Xandikos <calendar@example.com>"

Each switch has a matching ``XANDIKOS_*`` environment variable
(``XANDIKOS_IMIP_SEND``, ``XANDIKOS_SMTP_HOST``, ``XANDIKOS_SMTP_PORT``,
``XANDIKOS_SMTP_ENCRYPTION``, ``XANDIKOS_SMTP_USER``,
``XANDIKOS_SMTP_PASSWORD_FILE``, ``XANDIKOS_SMTP_FROM``) so Docker
deployments can configure outbound delivery without editing the
command line.

Inbound iMIP via ``xandikos import-imip``
-----------------------------------------

The CLI subcommand parses an iMIP message read from stdin and either
stores the iTIP payload directly in a principal's schedule inbox
(``-d``) or POSTs it to a running Xandikos over HTTP
(``--server-url`` or ``--principal-url``).

Sieve hookup
~~~~~~~~~~~~

Most deployments reach Xandikos via Dovecot Pigeonhole's
``vnd.dovecot.pipe`` extension. Pigeonhole only allows ``pipe`` to
binaries in ``sieve_pipe_bin_dir``, so install a small wrapper there:

.. code-block:: bash

   # /usr/lib/dovecot/sieve-pipe/xandikos-import-imip
   #!/bin/sh
   exec xandikos import-imip \
       --server-url http://localhost:8080/user/inbox/

Then drop a Sieve rule like the one in ``examples/sieve.example``:

.. code-block:: text

   require ["body", "copy", "vnd.dovecot.pipe"];

   if anyof (
       header :contains "Content-Type" "text/calendar",
       body :raw :contains "BEGIN:VCALENDAR"
   ) {
       pipe :copy "xandikos-import-imip";
   }

The ``:copy`` modifier is critical: without it, ``pipe`` *replaces*
mailbox delivery and the user's INBOX never sees the message.

For authenticated servers, point the wrapper at an HTTPS URL and
read a credential out of a tightly-permissioned password file:

.. code-block:: bash

   exec xandikos import-imip \
       --server-url https://dav.example.net/user/inbox/ \
       --username bob \
       --password-file /run/secrets/xandikos-password

Inbound iMIP via LMTP
---------------------

``xandikos serve --imip-listen`` exposes an LMTP endpoint that
accepts an iMIP message and stores its iTIP payload in the schedule
inbox of the principal configured with ``--current-user-principal``.
This avoids the wrapper script and lets MTAs talk to Xandikos
directly. Unix-socket targets are POSIX-only; on Windows, use
``host:port``. ``aiosmtpd`` is required:

.. code-block:: bash

   pip install 'xandikos[imip-listen]'

Run the listener on a unix socket (recommended) or a TCP port:

.. code-block:: bash

   xandikos serve --directory /var/lib/xandikos \
       --imip-listen=unix:/run/xandikos/imip.sock \
       --imip-listen-mode=660 \
       --imip-listen-group=postfix

Equivalent environment variables for Docker:
``XANDIKOS_IMIP_LISTEN``, ``XANDIKOS_IMIP_LISTEN_MODE``,
``XANDIKOS_IMIP_LISTEN_GROUP``.

Server-wide setup with Dovecot ``sieve_before``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The simplest hookup that scales to any number of users without
per-user state is a Pigeonhole ``sieve_before`` script (which runs
ahead of every user's personal Sieve script) plus a single Postfix
``transport_maps`` entry that routes a synthetic address to the
LMTP socket. New users are picked up automatically.

The full recipe — Sieve script, Pigeonhole config, Postfix
``main.cf`` / ``master.cf`` / transport map — is in
``examples/dovecot-lmtp.example``.

Address routing
~~~~~~~~~~~~~~~

The listener does not validate the LMTP envelope ``RCPT TO`` against
the principal's calendar-user-address-set. A ``serve`` instance has
exactly one principal, and the unix socket is the access boundary
(use ``--imip-listen-mode`` / ``--imip-listen-group`` to restrict it).
Recipients are logged for audit.

Inbound iMIP via Postfix milter
-------------------------------

``xandikos-milter`` is a standalone daemon that speaks the libmilter
wire protocol Postfix and Sendmail use to consult external filters at
SMTP intake. Every inbound message that flows through Postfix is
inspected; if it carries a valid iMIP REQUEST, REPLY or CANCEL, the
message is forwarded to a running Xandikos for storage in the schedule
inbox. The milter never modifies, defers or rejects messages, so
normal mailbox delivery is unaffected.

Compared to the LMTP listener-by-itself this needs no Sieve glue: as
soon as the milter is wired into ``smtpd_milters``, every iMIP message
that lands on the host is harvested. The cost is that the milter sees
*every* message Postfix accepts, so the per-message check has to stay
cheap (it does: a quick MIME walk that bails out as soon as no
``text/calendar`` part is found).

The milter does not touch the on-disk store directly — it always hands
off to a running Xandikos. Two transports are supported.

LMTP (same-host, recommended)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Pair the milter with the existing ``--imip-listen`` LMTP listener.
Postfix → milter → Xandikos LMTP is the cleanest setup when both
services run on the same host, and reuses the same code path the
existing Sieve hookup uses.

.. code-block:: bash

   xandikos serve \
       --directory /var/lib/xandikos \
       --imip-listen unix:/run/xandikos/imip.sock \
       --imip-listen-mode 660 \
       --imip-listen-group postfix

   xandikos-milter \
       --lmtp-socket unix:/run/xandikos/imip.sock \
       --listen unix:/run/xandikos/milter.sock \
       --listen-mode 660 \
       --listen-group postfix

HTTP (cross-host fallback)
~~~~~~~~~~~~~~~~~~~~~~~~~~

When the milter and Xandikos live on different hosts, point the milter
at the schedule-inbox URL over HTTPS, with HTTP Basic credentials read
from a tightly-permissioned password file:

.. code-block:: bash

   xandikos-milter \
       --server-url https://dav.example.net/user/inbox/ \
       --username calendar \
       --password-file /run/secrets/xandikos-password \
       --listen unix:/run/xandikos/milter.sock \
       --listen-mode 660 \
       --listen-group postfix

If Xandikos itself listens on a Unix socket on the same host as the
milter, pass ``--unix-socket`` to dial it directly without going via
TCP.

Postfix wiring
~~~~~~~~~~~~~~

Once the milter is running, point Postfix at it from ``main.cf``:

.. code-block:: text

   # /etc/postfix/main.cf
   smtpd_milters       = unix:/run/xandikos/milter.sock
   non_smtpd_milters   = $smtpd_milters
   milter_default_action = accept
   milter_protocol     = 6

   # Generous content timeout: the milter does an LMTP/HTTP round-trip
   # to Xandikos at end-of-message. The 300s default is usually fine.
   # milter_content_timeout = 300s

Apply with:

.. code-block:: bash

   sudo postconf -e \
       'smtpd_milters = unix:/run/xandikos/milter.sock' \
       'non_smtpd_milters = $smtpd_milters' \
       'milter_default_action = accept' \
       'milter_protocol = 6'
   sudo systemctl reload postfix

What each setting does:

``smtpd_milters``
    The list of milters Postfix consults for mail arriving via
    ``smtpd`` (the normal SMTP entry point — local *and* remote). The
    ``unix:`` prefix selects a local socket; ``inet:host:port`` is the
    TCP form.

``non_smtpd_milters``
    The list consulted for mail injected via ``sendmail(1)`` or the
    ``pickup`` service — cron jobs, ``mail`` from a shell, etc. Setting
    it to ``$smtpd_milters`` keeps iMIP coverage uniform regardless of
    how a message enters the queue.

``milter_default_action = accept``
    What Postfix does if the milter is unreachable or times out. The
    safe default is ``tempfail``, which defers the message; for an
    iMIP-harvesting milter that is far too aggressive — a Xandikos
    outage would block every inbound mail on the host. ``accept`` lets
    Postfix keep delivering normally and only loses the iMIP
    harvesting until the milter is back. **Always set this for
    xandikos-milter.**

``milter_protocol = 6``
    Pins the wire-protocol version Postfix offers. ``xandikos-milter``
    speaks up to v6; pinning it here keeps the negotiated version
    predictable across Postfix upgrades.

Socket permissions
""""""""""""""""""

Postfix runs as the ``postfix`` user (or ``smtpd``-chrooted into
``/var/spool/postfix``) and dials the Unix socket as that identity.
The ``--listen-mode 660 --listen-group postfix`` flags shown in the
recipes above give Postfix read/write access while keeping the socket
unreadable to anyone else. If your Postfix runs chrooted, place the
socket inside the Postfix queue directory (for example
``/var/spool/postfix/private/xandikos-milter``) and reference it
without the chroot prefix:

.. code-block:: text

   smtpd_milters = unix:private/xandikos-milter

Verifying
"""""""""

After ``postfix reload``, check the effective configuration:

.. code-block:: bash

   postconf -n | grep -E '^(smtpd|non_smtpd)_milters|^milter_'

Watch the milter's own log to confirm Postfix actually connects when
new mail arrives:

.. code-block:: text

   xandikos milter listening on unix:/run/xandikos/milter.sock, ...
   Forwarded iMIP REQUEST message <abc@host> to /run/xandikos/imip.sock ...

If the connection is failing, check ``journalctl -u postfix`` for
``connect to milter service`` errors — usually a socket-permission
problem.

Per-recipient scoping
"""""""""""""""""""""

A single ``xandikos-milter`` instance harvests iMIP for the principal
its transport is configured against. If your Postfix serves multiple
domains and only some should funnel iMIP into Xandikos, restrict the
milter with ``smtpd_milter_maps`` (Postfix ≥ 3.2):

.. code-block:: text

   # /etc/postfix/main.cf
   smtpd_milter_maps = inline:{ calendar.example.com = unix:/run/xandikos/milter.sock }

Mail to domains not listed in the map skips the milter entirely.

Reference
"""""""""

A copy-pasteable snippet lives in ``examples/postfix-milter.example``.
As with ``--imip-listen``, the milter's Unix socket is the access
boundary between Postfix and Xandikos: use ``--listen-mode`` /
``--listen-group`` to restrict who can talk to it. Equivalent
environment variables for Docker: ``XANDIKOS_MILTER_LMTP_SOCKET``,
``XANDIKOS_MILTER_SERVER_URL``, ``XANDIKOS_MILTER_LISTEN``,
``XANDIKOS_MILTER_LISTEN_MODE``, ``XANDIKOS_MILTER_LISTEN_GROUP``.

Loop avoidance
--------------

Xandikos sets ``Auto-Submitted: auto-generated`` (:rfc:`3834`) on
every iMIP message it emits. Both inbound paths skip messages that
arrive carrying any ``Auto-Submitted`` keyword other than ``no``,
so two Xandikos servers cannot bounce iTIP traffic between each
other indefinitely.

Troubleshooting
---------------

``xandikos import-imip`` and the LMTP listener log to stderr at
INFO level. Frequent log lines:

- ``Imported iMIP REQUEST message ... into /user/inbox/<name>``
  — success.
- ``Skipping auto-submitted message ...`` — RFC 3834 loop
  guard fired; expected for server-generated traffic.
- ``Invalid iMIP payload in ...`` — message had no usable
  ``text/calendar`` part. Recipient headers are logged so you can
  cross-check whether the wrong message is being piped.
- ``Failed to store iMIP ... in ...`` — storage error
  (permissions, disk full, locked git repo). Check the schedule
  inbox path on disk.

Smoke-test outbound delivery by inviting a remote attendee from a
client; check ``--debug`` logs for ``Sent iMIP REQUEST to ...``.
Smoke-test the LMTP listener with ``swaks`` or
``python -m smtplib``:

.. code-block:: bash

   swaks --to bob@example.com --from alice@example.com \
       --server /run/xandikos/imip.sock --protocol LMTP \
       --data path/to/sample.eml
