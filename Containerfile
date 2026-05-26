# Docker file for Xandikos.
#
# Note that this dockerfile starts Xandikos without any authentication;
# for authenticated access we recommend you run it behind a reverse proxy.
#
# Environment variables:
#   PORT - Port to listen on (default: 8000)
#   METRICS_PORT - Port for metrics endpoint (default: 8001)
#   LISTEN_ADDRESS - Address to bind to (default: 0.0.0.0)
#   DATA_DIR - Data directory path (default: /data)
#   CURRENT_USER_PRINCIPAL - User principal path (default: /user/)
#   ROUTE_PREFIX - URL route prefix (default: /)
#   AUTOCREATE - Auto-create directories (true/false)
#   DEFAULTS - Create default calendar/addressbook (true/false)
#   DEBUG - Enable debug logging (true/false)
#   DUMP_DAV_XML - Print DAV XML requests/responses (true/false)
#   NO_STRICT - Enable client compatibility workarounds (true/false)
#   AUTOCERT - Serve HTTPS with a self-signed certificate (true/false).
#              Development/testing only - use a reverse proxy with a real
#              CA-issued certificate for production.
#   HTPASSWD - Path to an Apache htpasswd file to require Basic auth.
#              Requires AUTOCERT=true. Mount the file read-only into the
#              container.
#   SOCKET_MODE - File mode (octal) for the web Unix socket. Only meaningful
#                 when LISTEN_ADDRESS is a path or unix:/...
#   SOCKET_GROUP - Group ownership for the web Unix socket.
#   IMIP_LISTEN - Enable the iMIP LMTP listener. Set to "auto" to bind
#                 unix:/sockets/imip.sock, or pass a full target such as
#                 "unix:/sockets/imip.sock" or "host:port".
#   IMIP_LISTEN_MODE / IMIP_LISTEN_GROUP - permissions for the LMTP socket.
#   MILTER_LISTEN - Enable the built-in Postfix/Sendmail milter. Set to
#                   "auto" to bind unix:/sockets/milter.sock, or pass a
#                   full target such as "unix:/sockets/milter.sock" or
#                   "host:port".
#   MILTER_LISTEN_MODE / MILTER_LISTEN_GROUP - permissions for the milter
#                   socket.
#
# Volumes:
#   /data    - calendar/contact storage (always required)
#   /sockets - where the web, iMIP and milter Unix sockets are created.
#              Bind-mount this from the host (e.g. /run/xandikos:/sockets)
#              to let host services (nginx, Postfix, ...) reach them.
#
# Command line arguments passed to the container override environment variables.

FROM debian:sid-slim
LABEL maintainer="jelmer@jelmer.uk"
RUN apt-get update && \
    apt-get -y install --no-install-recommends python3-icalendar python3-pip python3-jinja2 python3-defusedxml python3-aiohttp python3-vobject python3-aiohttp-openmetrics python3-qrcode python3-cryptography python3-bcrypt curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/ && \
    groupadd -g 1000 xandikos && \
    useradd -d /home/xandikos -c Xandikos -g xandikos -m -s /bin/bash -u 1000 xandikos && \
    # Install dulwich from pip instead of Debian package to get a newer version
    # that fixes _GitFile import issues in index.py (0.24.6 vs 0.24.2)
    pip3 install --break-system-packages dulwich
ADD . /code
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown xandikos:xandikos /entrypoint.sh && \
    mkdir -p /data /sockets && chown xandikos:xandikos /data /sockets
ENV PYTHONPATH=/code
VOLUME /data
# /sockets/ is where xandikos drops its UNIX domain sockets (web, iMIP
# LMTP, milter) when configured via LISTEN_ADDRESS / IMIP_LISTEN /
# MILTER_LISTEN. Bind-mount this from the host so the sockets are
# reachable from outside the container.
VOLUME /sockets
EXPOSE 8000 8001
USER xandikos
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD []
