# Xandikos
# Copyright (C) 2016-2018 Jelmer Vernooĳ <jelmer@jelmer.uk>, et al.
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

"""Xandikos command-line handling."""

import argparse
import asyncio
import logging
import posixpath
import sys
from urllib.parse import urlparse
from . import __version__
from .store import STORE_TYPE_CALENDAR, STORE_TYPE_ADDRESSBOOK


# If no subparser is given, default to 'serve'
def set_default_subparser(self, argv, name):
    subparser_found = False
    for arg in argv:
        if arg in ["-h", "--help", "--version"]:
            break
    else:
        for x in self._subparsers._actions:
            if not isinstance(x, argparse._SubParsersAction):
                continue
            for sp_name in x._name_parser_map.keys():
                if sp_name in argv:
                    subparser_found = True
        if not subparser_found:
            print('No subcommand given, defaulting to "%s"' % name)
            argv.insert(0, name)


def add_create_collection_parser(parser):
    """Add arguments for the create-collection subcommand."""
    parser.add_argument(
        "-d",
        "--directory",
        type=str,
        required=True,
        help="Root directory containing collections",
    )
    parser.add_argument(
        "--type",
        choices=["calendar", "addressbook"],
        required=True,
        help="Type of collection to create",
    )
    parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Name of the collection (used as path component)",
    )
    parser.add_argument(
        "--displayname", type=str, help="Display name for the collection"
    )
    parser.add_argument(
        "--description", type=str, help="Description for the collection"
    )
    parser.add_argument(
        "--color", type=str, help="Color for the collection (hex format, e.g., #FF0000)"
    )


def add_import_imip_parser(parser):
    """Add arguments for the import-imip subcommand."""
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "-d",
        "--directory",
        type=str,
        help="Root directory containing collections",
    )
    target.add_argument(
        "--server-url",
        type=str,
        help="Schedule inbox URL to POST the extracted iTIP text/calendar data to.",
    )
    parser.add_argument(
        "--principal",
        type=str,
        default="/user/",
        help="Principal path whose schedule inbox should receive the message. [%(default)s]",
    )
    parser.add_argument(
        "--autocreate",
        action="store_true",
        help="Create the principal, default calendar, and schedule inbox if missing.",
    )
    parser.add_argument(
        "--username",
        type=str,
        help="Username for HTTP Basic authentication with --server-url.",
    )
    parser.add_argument(
        "--password-file",
        type=str,
        help="File containing the HTTP Basic authentication password.",
    )


async def create_collection_main(args, parser):
    """Main function for the create-collection subcommand."""
    from .web import SingleUserFilesystemBackend

    logger = logging.getLogger(__name__)

    backend = SingleUserFilesystemBackend(args.directory)
    collection_path = args.name
    collection_type = (
        STORE_TYPE_CALENDAR if args.type == "calendar" else STORE_TYPE_ADDRESSBOOK
    )

    try:
        resource = backend.create_collection(collection_path)
    except FileExistsError:
        logger.error(f"Collection '{collection_path}' already exists")
        return 1

    resource.store.set_type(collection_type)

    if args.displayname:
        resource.store.set_displayname(args.displayname)

    if args.description:
        resource.store.set_description(args.description)

    if args.color:
        resource.store.set_color(args.color)

    logger.info(f"Successfully created {args.type} collection: {collection_path}")
    return 0


async def import_imip_main(args, parser, data: bytes | None = None):
    """Import a raw iMIP email message into a principal's schedule inbox."""
    from . import imip

    logger = logging.getLogger(__name__)

    if data is None:
        data = sys.stdin.buffer.read()
    try:
        payload = imip.extract_payload_from_bytes(data)
    except imip.InvalidIMIPMessage as exc:
        logger.error("Invalid iMIP message: %s", exc)
        return 1

    if args.server_url:
        return await _import_imip_to_server(args, payload)
    return await _import_imip_to_directory(args, payload)


async def _import_imip_to_directory(args, payload) -> int:
    from . import web

    logger = logging.getLogger(__name__)

    principal_path = _normalise_principal_path(args.principal)
    backend = web.SingleUserFilesystemBackend(args.directory)
    backend._mark_as_principal(principal_path)
    principal = backend.get_resource(principal_path)
    if principal is None:
        if not args.autocreate:
            logger.error(
                "Principal %s does not exist; pass --autocreate to create it.",
                principal_path,
            )
            return 1
        backend.create_principal(principal_path, create_defaults=True)
        principal = backend.get_resource(principal_path)

    if not isinstance(principal, web.Principal):
        logger.error("%s is not a principal.", principal_path)
        return 1

    inbox_path = posixpath.join(principal_path, principal.get_schedule_inbox_url())
    inbox = backend.get_resource(inbox_path)
    if not isinstance(inbox, web.ScheduleInbox) and args.autocreate:
        web.create_principal_defaults(backend, principal)
        inbox = backend.get_resource(inbox_path)
    if not isinstance(inbox, web.ScheduleInbox):
        logger.error(
            "%s is not a schedule inbox; pass --autocreate or create defaults first.",
            inbox_path,
        )
        return 1

    try:
        name, _etag = await inbox.create_member(
            None,
            [payload.calendar_data],
            "text/calendar",
            requester="xandikos import-imip",
        )
    except Exception as exc:
        logger.error("Unable to import iMIP message: %s", exc)
        return 1

    logger.info(
        "Imported iMIP %s message into %s/%s.",
        payload.method,
        inbox_path.rstrip("/"),
        name,
    )
    return 0


async def _import_imip_to_server(args, payload) -> int:
    logger = logging.getLogger(__name__)

    try:
        await _post_itip_to_server(
            args.server_url,
            payload.calendar_data,
            username=args.username,
            password=_read_password_file(args.password_file),
        )
    except Exception as exc:
        logger.error("Unable to POST iTIP message to %s: %s", args.server_url, exc)
        return 1

    logger.info("Posted iMIP %s message to %s.", payload.method, args.server_url)
    return 0


async def _post_itip_to_server(
    server_url: str,
    calendar_data: bytes,
    *,
    username: str | None = None,
    password: str | None = None,
) -> None:
    import aiohttp

    parsed = urlparse(server_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("server URL must use http or https")

    auth = None
    if username is not None:
        auth = aiohttp.BasicAuth(username, password or "")
    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(
            server_url,
            data=calendar_data,
            headers={"Content-Type": "text/calendar"},
        ) as response:
            if 200 <= response.status < 300:
                return
            body = await response.text()
            raise RuntimeError(
                "server returned HTTP %d %s: %s"
                % (response.status, response.reason, body.strip())
            )


def _read_password_file(path: str | None) -> str | None:
    if path is None:
        return None
    with open(path) as f:
        return f.read().strip()


def _normalise_principal_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return posixpath.normpath(path)


async def main(argv):
    # For now, just invoke xandikos.web
    from . import web
    from . import multi_user

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + ".".join(map(str, __version__)),
    )

    subparsers = parser.add_subparsers(help="Subcommands", dest="subcommand")
    web_parser = subparsers.add_parser(
        "serve", usage="%(prog)s -d ROOT-DIR [OPTIONS]", help="Run a Xandikos server"
    )
    web.add_parser(web_parser)

    create_parser = subparsers.add_parser(
        "create-collection", help="Create a calendar or address book collection"
    )
    add_create_collection_parser(create_parser)

    import_imip_parser = subparsers.add_parser(
        "import-imip",
        help="Import an iMIP email message from stdin into a schedule inbox",
    )
    add_import_imip_parser(import_imip_parser)

    multi_user_parser = subparsers.add_parser(
        "multi-user",
        usage="%(prog)s -d ROOT-DIR [OPTIONS]",
        help="Run a multi-user Xandikos server (experimental)",
    )
    multi_user.add_parser(multi_user_parser)
    subparsers.add_parser("help", help="Show this help message and exit")

    set_default_subparser(parser, argv, "serve")
    args = parser.parse_args(argv)

    if args.subcommand == "serve":
        return await web.main(args, parser)
    elif args.subcommand == "create-collection":
        # Configure logging for create-collection subcommand
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return await create_collection_main(args, parser)
    elif args.subcommand == "import-imip":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return await import_imip_main(args, parser)
    elif args.subcommand == "help":
        parser.print_help()
        return 0
    elif args.subcommand == "multi-user":
        logging.warn(
            "Multi-user mode is experimental, may not be stable and not yet provide sufficient isolation between users. Use with caution."
        )
        return await multi_user.main(args, parser)
    else:
        parser.print_help()
        return 1


def _get_package_versions() -> list[tuple[str, str]]:
    """Return versions of key packages for diagnostic output."""
    packages = ["xandikos", "dulwich", "vobject", "icalendar", "aiohttp", "defusedxml"]
    versions = []
    for pkg in packages:
        try:
            from importlib.metadata import version

            versions.append((pkg, version(pkg)))
        except Exception:
            versions.append((pkg, "not installed"))
    return versions


def cli_main():
    """Entry point for the command-line interface (for setuptools console_scripts)."""
    try:
        sys.exit(asyncio.run(main(sys.argv[1:])))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.stderr.write("\nInstalled package versions:\n")
        for pkg, ver in _get_package_versions():
            sys.stderr.write(f"  {pkg}: {ver}\n")
        sys.exit(1)


if __name__ == "__main__":
    cli_main()
