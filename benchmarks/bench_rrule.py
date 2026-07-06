# Xandikos
# Copyright (C) 2025-2026 Jelmer Vernooĳ <jelmer@jelmer.uk>, et al.
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

"""Indexed calendar-query benchmarks on recurring-event collections.

These exercise the KOrganizer/DAVx5 workload from PR #703. Each fixture
file has a single yearly-recurring VEVENT (a birthday). The client sends
open-ended time-range calendar-query REPORTs for VTODO, VJOURNAL, and
VEVENT.

Before PR #703:
  - VTODO/VJOURNAL queries triggered InsufficientIndexDataError for every
    file, forcing a full-file check and RRULE expansion.
  - VEVENT queries eagerly materialized up to MAX_RECURRENCE_INSTANCES
    (3000) occurrences per file before testing the first hit.

After PR #703 both paths are indexed and lazy, and these benchmarks drop
by two to three orders of magnitude.

Run:
    pytest benchmarks/ --benchmark-enable
    pytest benchmarks/ --benchmark-enable --benchmark-save=<label>
    pytest-benchmark compare <label1> <label2> --sort=fullname
"""

from datetime import datetime, timezone

from xandikos.icalendar import CalendarFilter, MAX_EXPANSION_TIME

from .conftest import BIRTHDAY_LARGE_COLLECTION, BIRTHDAY_SMALL_COLLECTION


def _make_open_ended_filter(comp_name):
    """Build an open-ended time-range filter for a given component type.

    The end is MAX_EXPANSION_TIME, matching the "no end" queries KOrganizer
    and DAVx5 issue for their VTODO/VJOURNAL/VEVENT calendar-query REPORTs.
    """
    f = CalendarFilter(timezone.utc)
    f.filter_subcomponent("VCALENDAR").filter_subcomponent(comp_name).filter_time_range(
        datetime(2026, 4, 6, 0, 4, tzinfo=timezone.utc),
        MAX_EXPANSION_TIME,
    )
    return f


class TestOpenEndedVTodoQuery:
    """Open-ended VTODO time-range against a birthday (VEVENT-only) collection.

    Before PR #703 every file fell back to a full-file check because the
    VTODO time-range matcher could not decide from indexes and raised
    InsufficientIndexDataError. Each fallback then expanded the yearly
    RRULE.
    """

    def _run(self, store):
        return list(store.iter_with_filter(_make_open_ended_filter("VTODO")))

    def test_bare_small(self, benchmark, bare_birthday_store_small):
        store, _ = bare_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_bare_large(self, benchmark, bare_birthday_store_large):
        store, _ = bare_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_tree_small(self, benchmark, tree_birthday_store_small):
        store, _ = tree_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_tree_large(self, benchmark, tree_birthday_store_large):
        store, _ = tree_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_memory_small(self, benchmark, memory_birthday_store_small):
        store, _ = memory_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_memory_large(self, benchmark, memory_birthday_store_large):
        store, _ = memory_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == 0


class TestOpenEndedVJournalQuery:
    """Open-ended VJOURNAL time-range against a birthday collection.

    Symmetric to the VTODO case and covered by the same match_indexes
    short-circuit.
    """

    def _run(self, store):
        return list(store.iter_with_filter(_make_open_ended_filter("VJOURNAL")))

    def test_bare_small(self, benchmark, bare_birthday_store_small):
        store, _ = bare_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_bare_large(self, benchmark, bare_birthday_store_large):
        store, _ = bare_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_tree_small(self, benchmark, tree_birthday_store_small):
        store, _ = tree_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_tree_large(self, benchmark, tree_birthday_store_large):
        store, _ = tree_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_memory_small(self, benchmark, memory_birthday_store_small):
        store, _ = memory_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == 0

    def test_memory_large(self, benchmark, memory_birthday_store_large):
        store, _ = memory_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == 0


class TestOpenEndedVEventQuery:
    """Open-ended VEVENT time-range against a birthday collection.

    Each file matches (the yearly RRULE hits the query window). Before
    PR #703 _get_occurrences_in_range materialized up to
    MAX_RECURRENCE_INSTANCES occurrences before testing the first hit;
    after the fix the underlying iterator is consumed lazily.
    """

    def _run(self, store):
        return list(store.iter_with_filter(_make_open_ended_filter("VEVENT")))

    def test_bare_small(self, benchmark, bare_birthday_store_small):
        store, _ = bare_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == BIRTHDAY_SMALL_COLLECTION

    def test_bare_large(self, benchmark, bare_birthday_store_large):
        store, _ = bare_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == BIRTHDAY_LARGE_COLLECTION

    def test_tree_small(self, benchmark, tree_birthday_store_small):
        store, _ = tree_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == BIRTHDAY_SMALL_COLLECTION

    def test_tree_large(self, benchmark, tree_birthday_store_large):
        store, _ = tree_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == BIRTHDAY_LARGE_COLLECTION

    def test_memory_small(self, benchmark, memory_birthday_store_small):
        store, _ = memory_birthday_store_small
        result = benchmark(self._run, store)
        assert len(result) == BIRTHDAY_SMALL_COLLECTION

    def test_memory_large(self, benchmark, memory_birthday_store_large):
        store, _ = memory_birthday_store_large
        result = benchmark(self._run, store)
        assert len(result) == BIRTHDAY_LARGE_COLLECTION
