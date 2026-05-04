# Xandikos
# Copyright (C) 2016-2017 Jelmer Vernooĳ <jelmer@jelmer.uk>, et al.
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

"""Tests for xandikos.__main__."""

import asyncio
import logging
import os
import shutil
import tempfile
import unittest
from email.message import EmailMessage
from unittest.mock import patch

from xandikos.__main__ import (
    add_create_collection_parser,
    add_import_imip_parser,
    create_collection_main,
    import_imip_main,
    main,
)
from xandikos.store import STORE_TYPE_ADDRESSBOOK, STORE_TYPE_CALENDAR
from xandikos.web import SingleUserFilesystemBackend


class CreateCollectionTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)

    def test_add_create_collection_parser(self):
        """Test that the create-collection parser is correctly configured."""
        import argparse

        parser = argparse.ArgumentParser()
        add_create_collection_parser(parser)

        # Test that required arguments are present
        import sys
        from io import StringIO

        old_stderr = sys.stderr
        sys.stderr = StringIO()  # Suppress argparse error output
        try:
            with self.assertRaises(SystemExit):
                parser.parse_args([])
        finally:
            sys.stderr = old_stderr

        # Test valid arguments
        args = parser.parse_args(
            ["-d", "/test/dir", "--type", "calendar", "--name", "test-cal"]
        )
        self.assertEqual(args.directory, "/test/dir")
        self.assertEqual(args.type, "calendar")
        self.assertEqual(args.name, "test-cal")
        self.assertIsNone(args.displayname)
        self.assertIsNone(args.description)
        self.assertIsNone(args.color)

        # Test with optional arguments
        args = parser.parse_args(
            [
                "-d",
                "/test/dir",
                "--type",
                "addressbook",
                "--name",
                "test-addr",
                "--displayname",
                "Test Address Book",
                "--description",
                "A test address book",
                "--color",
                "#FF0000",
            ]
        )
        self.assertEqual(args.displayname, "Test Address Book")
        self.assertEqual(args.description, "A test address book")
        self.assertEqual(args.color, "#FF0000")

    def test_create_collection_calendar_success(self):
        """Test successful creation of a calendar collection."""
        import argparse

        args = argparse.Namespace(
            directory=self.test_dir,
            type="calendar",
            name="test-calendar",
            displayname="Test Calendar",
            description="A test calendar",
            color="#FF5733",
        )

        result = asyncio.run(create_collection_main(args, None))
        self.assertEqual(result, 0)

        # Verify the collection was created
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "test-calendar")))

        # Verify the collection properties
        backend = SingleUserFilesystemBackend(self.test_dir)
        resource = backend.get_resource("/test-calendar")
        self.assertEqual(resource.store.get_type(), STORE_TYPE_CALENDAR)
        self.assertEqual(resource.store.get_displayname(), "Test Calendar")
        self.assertEqual(resource.store.get_description(), "A test calendar")
        self.assertEqual(resource.store.get_color(), "#FF5733")

    def test_create_collection_addressbook_success(self):
        """Test successful creation of an addressbook collection."""
        import argparse

        args = argparse.Namespace(
            directory=self.test_dir,
            type="addressbook",
            name="test-addressbook",
            displayname="Test Address Book",
            description=None,
            color=None,
        )

        result = asyncio.run(create_collection_main(args, None))
        self.assertEqual(result, 0)

        # Verify the collection was created
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "test-addressbook")))

        # Verify the collection properties
        backend = SingleUserFilesystemBackend(self.test_dir)
        resource = backend.get_resource("/test-addressbook")
        self.assertEqual(resource.store.get_type(), STORE_TYPE_ADDRESSBOOK)
        self.assertEqual(resource.store.get_displayname(), "Test Address Book")

    def test_create_collection_already_exists(self):
        """Test error handling when collection already exists."""
        import argparse

        args = argparse.Namespace(
            directory=self.test_dir,
            type="calendar",
            name="test-calendar",
            displayname=None,
            description=None,
            color=None,
        )

        # Create the collection first time
        result = asyncio.run(create_collection_main(args, None))
        self.assertEqual(result, 0)

        # Try to create again - should fail
        with self.assertLogs("xandikos.__main__", level=logging.ERROR) as cm:
            result = asyncio.run(create_collection_main(args, None))
            self.assertEqual(result, 1)
            self.assertIn("already exists", cm.output[0])

    def test_create_collection_minimal_args(self):
        """Test creating collection with only required arguments."""
        import argparse

        args = argparse.Namespace(
            directory=self.test_dir,
            type="calendar",
            name="minimal-cal",
            displayname=None,
            description=None,
            color=None,
        )

        result = asyncio.run(create_collection_main(args, None))
        self.assertEqual(result, 0)

        # Verify the collection was created
        backend = SingleUserFilesystemBackend(self.test_dir)
        resource = backend.get_resource("/minimal-cal")
        self.assertEqual(resource.store.get_type(), STORE_TYPE_CALENDAR)


REQUEST = b"""\
BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Test//EN\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:imip-request@example.com\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:Imported invite\r
ORGANIZER:mailto:alice@example.com\r
ATTENDEE:mailto:bob@example.com\r
END:VEVENT\r
END:VCALENDAR\r
"""


def _imip_message(calendar_data=REQUEST, method="REQUEST"):
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>"
    msg.set_content(
        calendar_data.decode("utf-8"),
        subtype="calendar",
        charset="utf-8",
        params={"method": method},
    )
    return msg.as_bytes()


class ImportIMIPTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)

    def _args(self, **kwargs):
        import argparse

        values = {
            "directory": self.test_dir,
            "server_url": None,
            "principal": "/user/",
            "autocreate": False,
            "username": None,
            "password_file": None,
        }
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_add_import_imip_parser(self):
        import argparse
        import sys
        from io import StringIO

        parser = argparse.ArgumentParser()
        add_import_imip_parser(parser)

        old_stderr = sys.stderr
        sys.stderr = StringIO()
        try:
            with self.assertRaises(SystemExit):
                parser.parse_args([])
        finally:
            sys.stderr = old_stderr

        args = parser.parse_args(["-d", "/test/dir", "--principal", "/alice/"])
        self.assertEqual("/test/dir", args.directory)
        self.assertIsNone(args.server_url)
        self.assertEqual("/alice/", args.principal)
        self.assertFalse(args.autocreate)

        args = parser.parse_args(
            [
                "--server-url",
                "https://dav.example/user/inbox/",
                "--username",
                "bob",
                "--password-file",
                "/run/secrets/xandikos-password",
            ]
        )
        self.assertIsNone(args.directory)
        self.assertEqual("https://dav.example/user/inbox/", args.server_url)
        self.assertEqual("bob", args.username)
        self.assertEqual("/run/secrets/xandikos-password", args.password_file)

    def test_import_imip_autocreates_and_applies_request(self):
        result = asyncio.run(
            import_imip_main(
                self._args(autocreate=True),
                None,
                data=_imip_message(),
            )
        )

        self.assertEqual(0, result)
        backend = SingleUserFilesystemBackend(self.test_dir)
        backend._mark_as_principal("/user")
        inbox = backend.get_resource("/user/inbox")
        calendar = backend.get_resource("/user/calendars/calendar")

        self.assertEqual(1, len(list(inbox.members())))
        calendar_members = list(calendar.members())
        self.assertEqual(1, len(calendar_members))
        name, member = calendar_members[0]
        file = calendar.store.get_file(name, member.get_content_type(), member.etag)
        body = b"".join(file.content)
        self.assertIn(b"UID:imip-request@example.com", body)
        self.assertNotIn(b"METHOD:REQUEST", body)

    def test_import_imip_requires_existing_principal_without_autocreate(self):
        with self.assertLogs("xandikos.__main__", level=logging.ERROR):
            result = asyncio.run(
                import_imip_main(self._args(), None, data=_imip_message())
            )

        self.assertEqual(1, result)

    def test_import_imip_rejects_invalid_message(self):
        with self.assertLogs("xandikos.__main__", level=logging.ERROR):
            result = asyncio.run(
                import_imip_main(
                    self._args(autocreate=True),
                    None,
                    data=b"Subject: nope\r\n\r\nhello",
                )
            )

        self.assertEqual(1, result)

    def test_import_imip_posts_extracted_itip_to_server(self):
        captured = {}

        async def post_itip_to_server(url, calendar_data, *, username, password):
            captured["url"] = url
            captured["calendar_data"] = calendar_data
            captured["username"] = username
            captured["password"] = password

        password_file = os.path.join(self.test_dir, "password")
        with open(password_file, "w") as f:
            f.write("secret\n")

        with patch("xandikos.__main__._post_itip_to_server", post_itip_to_server):
            result = asyncio.run(
                import_imip_main(
                    self._args(
                        directory=None,
                        server_url="https://dav.example/user/inbox/",
                        username="bob",
                        password_file=password_file,
                    ),
                    None,
                    data=_imip_message(),
                )
            )

        self.assertEqual(0, result)
        self.assertEqual("https://dav.example/user/inbox/", captured["url"])
        self.assertIn(b"METHOD:REQUEST", captured["calendar_data"])
        self.assertEqual("bob", captured["username"])
        self.assertEqual("secret", captured["password"])

    def test_import_imip_reports_server_post_failure(self):
        async def post_itip_to_server(url, calendar_data, *, username, password):
            raise RuntimeError("boom")

        with patch("xandikos.__main__._post_itip_to_server", post_itip_to_server):
            with self.assertLogs("xandikos.__main__", level=logging.ERROR):
                result = asyncio.run(
                    import_imip_main(
                        self._args(
                            directory=None,
                            server_url="https://dav.example/user/inbox/",
                        ),
                        None,
                        data=_imip_message(),
                    )
                )

        self.assertEqual(1, result)


class MainCommandTests(unittest.TestCase):
    def test_main_create_collection_subcommand(self):
        """Test that the main function recognizes create-collection subcommand."""
        with tempfile.TemporaryDirectory() as test_dir:
            with self.assertLogs("xandikos.__main__", level=logging.INFO) as cm:
                result = asyncio.run(
                    main(
                        [
                            "create-collection",
                            "-d",
                            test_dir,
                            "--type",
                            "calendar",
                            "--name",
                            "test-cal",
                        ]
                    )
                )
                self.assertEqual(result, 0)
                self.assertIn("Successfully created", cm.output[0])

    def test_main_help_includes_create_collection(self):
        """Test that help includes the create-collection subcommand."""
        import sys
        from io import StringIO

        # Capture stdout since argparse writes help there
        old_stdout = sys.stdout
        captured_output = StringIO()
        sys.stdout = captured_output

        try:
            with self.assertRaises(SystemExit):
                asyncio.run(main(["--help"]))
        finally:
            sys.stdout = old_stdout

        # Check that help was printed and includes create-collection
        help_output = captured_output.getvalue()
        self.assertIn("create-collection", help_output)
        self.assertIn("import-imip", help_output)

    def test_main_invalid_subcommand(self):
        """Test handling of invalid subcommands."""
        # Note: Due to the default subparser mechanism, unknown commands
        # get passed to the 'serve' subcommand and cause argument errors.
        # This test verifies that the system exits with an error code.
        import sys
        from io import StringIO

        old_stderr = sys.stderr
        sys.stderr = StringIO()  # Suppress argparse error output
        try:
            with patch("builtins.print"):
                with self.assertRaises(SystemExit) as cm:
                    asyncio.run(main(["invalid-command"]))
                # Expect exit code 2 (argparse error)
                self.assertEqual(cm.exception.code, 2)
        finally:
            sys.stderr = old_stderr

    def test_main_create_collection_help(self):
        """Test create-collection subcommand help."""
        import sys
        from io import StringIO

        # Capture stdout since argparse writes help there
        old_stdout = sys.stdout
        captured_output = StringIO()
        sys.stdout = captured_output

        try:
            with self.assertRaises(SystemExit):
                asyncio.run(main(["create-collection", "--help"]))
        finally:
            sys.stdout = old_stdout

        # Check that help was printed and includes expected options
        help_output = captured_output.getvalue()
        self.assertIn("--type", help_output)
        self.assertIn("--name", help_output)
        self.assertIn("--displayname", help_output)

    def test_main_import_imip_help(self):
        """Test import-imip subcommand help."""
        import sys
        from io import StringIO

        old_stdout = sys.stdout
        captured_output = StringIO()
        sys.stdout = captured_output

        try:
            with self.assertRaises(SystemExit):
                asyncio.run(main(["import-imip", "--help"]))
        finally:
            sys.stdout = old_stdout

        help_output = captured_output.getvalue()
        self.assertIn("--principal", help_output)
        self.assertIn("--autocreate", help_output)
        self.assertIn("--server-url", help_output)
        self.assertIn("--password-file", help_output)

    def test_main_help_subcommand(self):
        """Test that 'help' subcommand prints usage and returns 0."""
        import sys
        from io import StringIO

        old_stdout = sys.stdout
        captured_output = StringIO()
        sys.stdout = captured_output

        try:
            result = asyncio.run(main(["help"]))
        finally:
            sys.stdout = old_stdout

        self.assertEqual(result, 0)
        help_output = captured_output.getvalue()
        # Should list all subcommands
        self.assertIn("serve", help_output)
        self.assertIn("create-collection", help_output)
        self.assertIn("import-imip", help_output)
        self.assertIn("help", help_output)
