Configuration Reference
=======================

This page provides a comprehensive reference for all Xandikos configuration options.

Command-Line Options
--------------------

Basic Options
~~~~~~~~~~~~~

``-d, --directory``
    Root directory to serve DAV collections from (required).

    Example: ``--directory /var/lib/xandikos``

``-p, --port``
    Port to listen on (default: 8080).

    Example: ``--port 8090``

``-l, --listen-address``
    Address to listen on (default: localhost). Can also be a Unix socket path.

    Example: ``--listen-address 0.0.0.0``
    Example: ``--listen-address /run/xandikos/web.sock``

``--route-prefix``
    Path prefix for the application. Use this when running behind a reverse proxy on a subpath.

    Example: ``--route-prefix /dav``

``--autocert``
    Serve HTTPS using a self-signed certificate. The certificate and private
    key are generated on first use and stored under
    ``~/.local/share/xandikos/certs`` (respecting ``XDG_DATA_HOME``). The
    certificate is regenerated automatically when it is within 30 days of
    expiring.

    This option exists for development and testing. **Do not use it in
    production.** Xandikos itself is not hardened for direct exposure to
    the internet (no authentication, no rate limiting) and clients will not
    trust a self-signed certificate. For production, run Xandikos behind a
    reverse proxy that handles authentication and terminates TLS with a
    certificate from a trusted CA such as Let's Encrypt - see
    :ref:`reverse-proxy`.

    Requires the ``cryptography`` Python package.

``--state-dir DIR``
    Directory for server state (TLS certificates from ``--autocert``,
    VAPID keys for ``--webdav-push``, the push subscription index).
    Distinct from ``--directory``, which stores user calendars and
    address books.

    Defaults to ``$XDG_DATA_HOME/xandikos`` (typically
    ``~/.local/share/xandikos``).

``--webdav-push``
    Enable WebDAV-Push (`draft-bitfire-webdav-push
    <https://github.com/bitfireAT/webdav-push/>`_). Clients - notably
    DAVx5 - can register a Web Push (:RFC:`8030`) endpoint instead of
    polling; Xandikos delivers VAPID-signed (:RFC:`8292`),
    ``aes128gcm``-encrypted (:RFC:`8291`) notifications when a
    calendar or addressbook changes.

    A VAPID keypair is generated under ``<state-dir>/vapid/`` on first
    start and persisted across restarts. Per-collection subscription
    state lives next to the collection on disk and is excluded from
    Git history.

    Requires the optional ``webdav-push`` extra::

        pip install 'xandikos[webdav-push]'

``--htpasswd FILE``
    Require HTTP Basic authentication, validating credentials against an
    Apache-style ``htpasswd`` file. The file is reloaded automatically when
    its modification time changes, so users can be added or removed
    without restarting the server.

    Supported hash formats are bcrypt (recommended; create with
    ``htpasswd -B``) and SHA1. ``$apr1$`` and traditional crypt entries
    are rejected.

    **Requires ``--autocert``.** Basic authentication transmits credentials
    in cleartext and must not be served over plain HTTP. If you run
    Xandikos behind a reverse proxy, configure authentication at the
    proxy instead of using this flag.

    Requires the ``bcrypt`` Python package when verifying bcrypt entries.

    Example::

        htpasswd -B -c /etc/xandikos/htpasswd alice
        xandikos --autocert --htpasswd /etc/xandikos/htpasswd \\
                 -d /var/lib/xandikos


Collection Management
~~~~~~~~~~~~~~~~~~~~~

``--defaults``
    Create default calendar and addressbook collections if they don't exist.
    Collections created:

    - ``calendars/calendar`` - Default calendar
    - ``contacts/addressbook`` - Default addressbook

``--autocreate``
    Automatically create missing directories when accessed.
    Options:

    - ``yes`` - Create all missing directories
    - ``no`` - Never create directories (default)

    Example: ``--autocreate yes``

Authentication and Permissions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``--current-user-principal``
    Path to current user principal (default: /user/).

    Example: ``--current-user-principal /alice``

Debugging Options
~~~~~~~~~~~~~~~~~

``--debug``
    Enable debug logging. Shows detailed internal operations.

``--dump-dav-xml``
    Dump all WebDAV request/response XML to stdout. Useful for debugging client issues.

``--no-strict``
    Don't be strict about WebDAV compliance. Enable workarounds for broken clients.

Service Discovery
~~~~~~~~~~~~~~~~~

``--avahi``
    Announce services with Avahi/Bonjour for automatic discovery.

``--metrics-port``
    Port to listen on for metrics endpoint.

    Example: ``--metrics-port 9090``

System Integration
~~~~~~~~~~~~~~~~~~

``--no-detect-systemd``
    Disable systemd socket activation detection.

Docker Environment Variables
----------------------------

When running in Docker, these environment variables are supported:

``CURRENT_USER_PRINCIPAL``
    Path to current user principal (default: ``/$USER``).

``AUTOCREATE``
    Whether to autocreate collections. Options: ``defaults``, ``empty``.

    * ``defaults`` - Create default collections
    * ``empty`` - Create principal without collections
    * ``no`` - Do not create collections (default)

``ROUTE_PREFIX``
    HTTP path prefix for the application.

``XANDIKOS_LISTEN_ADDRESS``
    Address to bind to (default: ``localhost``, ``0.0.0.0`` in Docker).

``XANDIKOS_PORT``
    Port to listen on (default: ``8080``).

``AUTOCERT``
    Set to ``true`` or ``1`` to serve HTTPS with a self-signed certificate
    (development/testing only - see ``--autocert`` above). Certificates are
    written under the home directory of the container user; mount a volume
    at ``/code/.local/share/xandikos/certs`` if you want them to persist
    across container restarts.

``HTPASSWD``
    Path inside the container to an Apache-style htpasswd file used to
    require HTTP Basic authentication (see ``--htpasswd`` above). Mount
    the file read-only at this path. Requires ``AUTOCERT=true``.

``WEBDAV_PUSH``
    Controls whether WebDAV-Push is enabled. Enabled by default in the
    container image; set to ``false`` or ``0`` to disable. See
    ``--webdav-push`` above.

``STATE_DIR``
    Directory for server state (VAPID keys, TLS certificates, push
    subscription index). Defaults to ``/data/state`` inside the
    container so it persists on the ``/data`` volume. See
    ``--state-dir`` above.

Configuration Examples
----------------------

Basic Standalone Server
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   xandikos \
     --directory /var/lib/xandikos \
     --defaults \
     --current-user-principal /john

Behind nginx on Subpath
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   xandikos \
     --directory /var/lib/xandikos \
     --route-prefix /dav \
     --listen-address /run/xandikos/web.sock \
     --defaults

Production with Logging
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   xandikos \
     --directory /var/lib/xandikos \
     --listen-address localhost \
     --port 8080 \
     --debug \
     --defaults

Docker Compose Configuration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   version: '3'
   services:
     xandikos:
       image: ghcr.io/jelmer/xandikos:latest
       environment:
         - AUTOCREATE=defaults
         - CURRENT_USER_PRINCIPAL=/alice
         - ROUTE_PREFIX=/dav
       volumes:
         - ./data:/data
       ports:
         - "127.0.0.1:8080:8080"

Systemd Socket Activation
~~~~~~~~~~~~~~~~~~~~~~~~~

Create ``/etc/systemd/system/xandikos.socket``:

.. code-block:: ini

   [Unit]
   Description=Xandikos CalDAV/CardDAV server socket

   [Socket]
   # All Xandikos sockets live under /run/xandikos/ so the web,
   # milter and iMIP LMTP sockets share one directory.
   ListenStream=/run/xandikos/web.sock
   RuntimeDirectory=xandikos

   [Install]
   WantedBy=sockets.target

Create ``/etc/systemd/system/xandikos.service``:

.. code-block:: ini

   [Unit]
   Description=Xandikos CalDAV/CardDAV server
   After=network.target

   [Service]
   Type=notify
   ExecStart=/usr/bin/xandikos \
     --directory /var/lib/xandikos \
     --listen-address /run/xandikos/web.sock \
     --defaults
   User=xandikos
   Group=xandikos

   [Install]
   WantedBy=multi-user.target

Per-Principal Configuration
---------------------------

A principal's settings live in a ``.xandikos`` INI file directly under
the principal directory. The same file holds scheduling settings such as
``calendar-user-address-set`` and the calendar/addressbook home-set
overrides.

To advertise calendar and addressbook home collections at non-default
paths for a principal, drop a ``.xandikos`` file under the principal
directory before the home directories are created:

.. code-block:: ini

   [principal]
   calendar-home-set = my-calendars
   addressbook-home-set = my-contacts

Both keys accept a comma-separated list to advertise multiple homes.
When unset, the defaults ``calendars`` and ``contacts`` apply.

Scheduling Identity (calendar-user-address-set)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``calendar-user-address-set`` (:RFC:`6638`, section 2.4.1) lists the
addresses by which a principal is known as a calendar user. Clients use
it to recognise which invitations belong to you and to fill in the
organiser and attendee fields, and Xandikos uses it to route incoming
iTIP/iMIP messages to the right principal.

Set it in the ``[scheduling]`` section of the principal's ``.xandikos``
file. ``addresses`` is a comma-separated list, conventionally
``mailto:`` URIs:

.. code-block:: ini

   [scheduling]
   addresses = mailto:alice@example.com, mailto:alice@work.example.org

The same section also holds the related ``user-type`` (one of
``INDIVIDUAL``, ``GROUP``, ``RESOURCE``, ``ROOM`` or ``UNKNOWN``) and
``default-calendar-url`` (the calendar that receives incoming iTIP
messages).

When no ``addresses`` key is set and Xandikos is running in single user mode,
it falls back to a single address derived from the ``EMAIL`` environment
variable of the server process.

The property can also be read and written over the wire with a WebDAV
PROPPATCH against the principal URL, so clients that support it can
manage the set without editing the file directly.

Directory Structure
-------------------

Xandikos organizes data in the following directory structure:

.. code-block:: text

   /var/lib/xandikos/           # Root directory (configured with --directory)
   ├── calendars/               # Calendar collections
   │   ├── calendar/            # Default calendar
   │   │   ├── .git/            # Git repository
   │   │   └── *.ics            # iCalendar files
   │   └── tasks/               # Task list
   └── contacts/                # Addressbook collections
       └── addressbook/         # Default addressbook
           ├── .git/            # Git repository
           └── *.vcf            # vCard files

File Naming
~~~~~~~~~~~

- Calendar events: ``{UID}.ics``
- Contacts: ``{UID}.vcf``
- UIDs are automatically generated if not provided

Git Storage
~~~~~~~~~~~

Each collection is stored as a Git repository, providing:

- Version history for all changes
- Ability to revert changes
- Efficient storage of modifications
- Built-in backup mechanism
