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

"""Tests for RFC 3744 principal-property-search REPORT."""

import asyncio
import shutil
import tempfile
import unittest

from xandikos import webdav
from xandikos.web import SingleUserFilesystemBackend, XandikosApp
from xandikos.webdav import ET

try:
    from xandikos.multi_user import MultiUserFilesystemBackend
except ImportError:  # pragma: no cover
    MultiUserFilesystemBackend = None


class DisplayNameProperty(webdav.Property):
    name = "{DAV:}displayname"

    async def get_value(self, href, resource, el, environ):
        el.text = resource.get_displayname()


PROPERTIES = {"{DAV:}displayname": DisplayNameProperty()}


class FakePrincipal(webdav.Principal):
    resource_types = webdav.Resource.resource_types + [webdav.PRINCIPAL_RESOURCE_TYPE]

    def __init__(self, displayname) -> None:
        self._displayname = displayname

    def get_displayname(self):
        return self._displayname


class FakeCollection(webdav.Collection):
    resource_types = webdav.Resource.resource_types + [webdav.COLLECTION_RESOURCE_TYPE]

    def __init__(self, members) -> None:
        self._members = members

    def members(self):
        return list(self._members.items())

    def get_member(self, name):
        return self._members[name]


def build_body(
    searches,
    requested_props=("{DAV:}displayname",),
    test=None,
    apply_to_collection_set=False,
):
    body = ET.Element("{DAV:}principal-property-search")
    if test is not None:
        body.set("test", test)
    for prop_name, match in searches:
        ps = ET.SubElement(body, "{DAV:}property-search")
        prop = ET.SubElement(ps, "{DAV:}prop")
        ET.SubElement(prop, prop_name)
        ET.SubElement(ps, "{DAV:}match").text = match
    prop = ET.SubElement(body, "{DAV:}prop")
    for name in requested_props:
        ET.SubElement(prop, name)
    if apply_to_collection_set:
        ET.SubElement(body, "{DAV:}apply-to-principal-collection-set")
    return body


class FakeRoot(webdav.Resource):
    """Root-like resource that enumerates principals via iter_principals."""

    def __init__(self, principals) -> None:
        self._principals = principals

    def iter_principals(self):
        return list(self._principals.items())


def run_report(collection, body, environ=None, strict=True):
    reporter = webdav.PrincipalPropertySearchReporter()

    async def go():
        return await reporter.report(
            environ=environ or {},
            request_body=body,
            resources_by_hrefs=lambda hrefs: [],
            properties=PROPERTIES,
            base_href="/principals/",
            resource=collection,
            depth="0",
            strict=strict,
        )

    response = asyncio.run(go())
    return ET.fromstring(b"".join(response.body))


def matched_hrefs(root):
    return sorted(r.find("{DAV:}href").text for r in root.findall("{DAV:}response"))


class PrincipalPropertySearchReporterTests(unittest.TestCase):
    def setUp(self):
        self.collection = FakeCollection(
            {
                "alice": FakePrincipal("Alice Smith"),
                "bob": FakePrincipal("Bob Jones"),
                "carol": FakePrincipal("Carol Smith"),
            }
        )

    def test_reporter_name(self):
        reporter = webdav.PrincipalPropertySearchReporter()
        self.assertEqual(reporter.name, "{DAV:}principal-property-search")

    def test_substring_match(self):
        body = build_body([("{DAV:}displayname", "smith")])
        root = run_report(self.collection, body)
        self.assertEqual(
            ["/principals/alice", "/principals/carol"], matched_hrefs(root)
        )

    def test_no_match(self):
        body = build_body([("{DAV:}displayname", "nobody")])
        root = run_report(self.collection, body)
        self.assertEqual([], matched_hrefs(root))

    def test_returns_requested_property(self):
        body = build_body([("{DAV:}displayname", "Bob")])
        root = run_report(self.collection, body)
        responses = root.findall("{DAV:}response")
        self.assertEqual(1, len(responses))
        prop = responses[0].find("{DAV:}propstat").find("{DAV:}prop")
        self.assertEqual("Bob Jones", prop.find("{DAV:}displayname").text)

    def test_allof(self):
        body = build_body(
            [("{DAV:}displayname", "smith"), ("{DAV:}displayname", "alice")],
            test="allof",
        )
        root = run_report(self.collection, body)
        self.assertEqual(["/principals/alice"], matched_hrefs(root))

    def test_anyof(self):
        body = build_body(
            [("{DAV:}displayname", "bob"), ("{DAV:}displayname", "carol")],
            test="anyof",
        )
        root = run_report(self.collection, body)
        self.assertEqual(["/principals/bob", "/principals/carol"], matched_hrefs(root))

    def test_invalid_test_attribute(self):
        body = build_body([("{DAV:}displayname", "x")], test="bogus")
        with self.assertRaises(webdav.BadRequestError):
            run_report(self.collection, body)

    def test_no_property_search(self):
        body = ET.Element("{DAV:}principal-property-search")
        ET.SubElement(body, "{DAV:}prop")
        with self.assertRaises(webdav.BadRequestError):
            run_report(self.collection, body)

    def test_non_principals_ignored(self):
        collection = FakeCollection({"cal": FakeCollection({})})
        body = build_body([("{DAV:}displayname", "cal")])
        root = run_report(collection, body)
        self.assertEqual([], matched_hrefs(root))

    def test_unknown_tag_strict(self):
        body = build_body([("{DAV:}displayname", "smith")])
        ET.SubElement(body, "{DAV:}bogus")
        with self.assertRaises(webdav.BadRequestError):
            run_report(self.collection, body)

    def test_unknown_tag_non_strict(self):
        body = build_body([("{DAV:}displayname", "smith")])
        ET.SubElement(body, "{DAV:}bogus")
        root = run_report(self.collection, body, strict=False)
        self.assertEqual(
            ["/principals/alice", "/principals/carol"], matched_hrefs(root)
        )


class IterPrincipalsFastPathTests(unittest.TestCase):
    """Tests for the backend iter_principals fast path."""

    def setUp(self):
        self.root = FakeRoot(
            {
                "/user": FakePrincipal("Alice Smith"),
                "/users/bob": FakePrincipal("Bob Jones"),
            }
        )

    def test_uses_iter_principals(self):
        body = build_body([("{DAV:}displayname", "smith")])
        root = run_report(self.root, body)
        self.assertEqual(["/user/"], matched_hrefs(root))

    def test_prefixes_script_name(self):
        body = build_body([("{DAV:}displayname", "bob")])
        root = run_report(self.root, body, environ={"SCRIPT_NAME": "/dav"})
        self.assertEqual(["/dav/users/bob/"], matched_hrefs(root))


def report_against_root(app, body, environ=None):
    """Drive the principal-property-search reporter against the root."""
    import functools

    from xandikos.webdav import _get_resources_by_hrefs

    reporter = app.reporters["{DAV:}principal-property-search"]
    root = app.backend.get_resource("/")
    env = {"SCRIPT_NAME": ""}
    if environ:
        env.update(environ)
    resources_by_hrefs = functools.partial(_get_resources_by_hrefs, app.backend, env)

    async def go():
        return await reporter.report(
            environ=env,
            request_body=body,
            resources_by_hrefs=resources_by_hrefs,
            properties=app.properties,
            base_href="/",
            resource=root,
            depth="0",
            strict=True,
        )

    response = asyncio.run(go())
    return ET.fromstring(b"".join(response.body))


class SingleUserEndToEndTests(unittest.TestCase):
    """End-to-end test through a real single-user backend."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.backend = SingleUserFilesystemBackend(self.test_dir, autocreate=True)
        self.backend.create_principal("/user", create_defaults=True)
        self.app = XandikosApp(self.backend, current_user_principal="/user")

    def test_matches_principal(self):
        # PrincipalBare falls back to the path basename for displayname.
        body = build_body([("{DAV:}displayname", "user")])
        root = report_against_root(self.app, body)
        self.assertEqual(["/user/"], matched_hrefs(root))

    def test_no_match(self):
        body = build_body([("{DAV:}displayname", "nobody")])
        root = report_against_root(self.app, body)
        self.assertEqual([], matched_hrefs(root))

    def test_apply_to_principal_collection_set(self):
        body = build_body([("{DAV:}displayname", "user")], apply_to_collection_set=True)
        root = report_against_root(self.app, body)
        self.assertEqual(["/user/"], matched_hrefs(root))


@unittest.skipIf(MultiUserFilesystemBackend is None, "multi_user backend not available")
class MultiUserEndToEndTests(unittest.TestCase):
    """End-to-end test through a real multi-user backend."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.backend = MultiUserFilesystemBackend(self.test_dir)
        for user in ("alice", "bob"):
            self.backend.set_principal(user)
        self.app = XandikosApp(self.backend, current_user_principal="/%(REMOTE_USER)s")

    def test_matches_single_principal(self):
        # PrincipalBare falls back to the path basename for displayname.
        body = build_body([("{DAV:}displayname", "alice")])
        root = report_against_root(self.app, body)
        self.assertEqual(["/alice/"], matched_hrefs(root))

    def test_matches_via_anyof(self):
        body = build_body(
            [("{DAV:}displayname", "alice"), ("{DAV:}displayname", "bob")],
            test="anyof",
        )
        root = report_against_root(self.app, body)
        self.assertEqual(["/alice/", "/bob/"], matched_hrefs(root))

    def test_apply_to_principal_collection_set(self):
        # With the flag, the search resolves the principal-collection-set
        # (the root) and searches its principals regardless of request-URI.
        body = build_body(
            [("{DAV:}displayname", "alice")], apply_to_collection_set=True
        )
        root = report_against_root(self.app, body)
        self.assertEqual(["/alice/"], matched_hrefs(root))


def get_property(app, resource_path, name, environ=None):
    """Fetch a single property value element from a resource."""
    resource = app.backend.get_resource(resource_path)
    env = {"SCRIPT_NAME": ""}
    if environ:
        env.update(environ)

    async def go():
        return await webdav.get_property_from_name(
            resource_path, resource, app.properties, name, env
        )

    return asyncio.run(go())


class PrincipalCollectionSetPropertyTests(unittest.TestCase):
    """Tests for the DAV:principal-collection-set property."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.backend = SingleUserFilesystemBackend(self.test_dir, autocreate=True)
        self.backend.create_principal("/user", create_defaults=True)
        self.app = XandikosApp(self.backend, current_user_principal="/user")

    def _hrefs(self, propstat):
        return sorted(
            child.text for child in propstat.prop if child.tag == "{DAV:}href"
        )

    def test_returns_parent_collection(self):
        propstat = get_property(self.app, "/user/", "{DAV:}principal-collection-set")
        self.assertEqual("200 OK", propstat.statuscode)
        self.assertEqual(["/"], self._hrefs(propstat))

    def test_prefixes_script_name(self):
        propstat = get_property(
            self.app,
            "/user/",
            "{DAV:}principal-collection-set",
            environ={"SCRIPT_NAME": "/dav"},
        )
        self.assertEqual(["/dav/"], self._hrefs(propstat))


if __name__ == "__main__":
    unittest.main()
