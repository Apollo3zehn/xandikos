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
     Sieve script can deliver to directly.

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
