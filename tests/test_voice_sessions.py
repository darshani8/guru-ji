"""Voice sessions shared through the control store, as on several API tasks."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.persistence.database import InMemoryControlStore, SqliteControlStore, timestamp
from app.voice.session_manager import VoiceScopeError, VoiceSessionManager


def _person(principal_id: str = "stu-1", college: str = "college_a") -> Principal:
    return Principal(
        principal_id, PrincipalType.STUDENT,
        frozenset({Capability.START_VOICE_SESSION, Capability.ASK_READ_ONLY, Capability.AGENT_COMMAND}),
        (InstitutionScope(college),), True,
    )


class SharedVoiceSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SqliteControlStore(":memory:")
        # Two API tasks: each has its own manager, both share the database.
        self.task_a = VoiceSessionManager(ttl_seconds=600, max_active=10, store=self.store, ticket_ttl_seconds=30)
        self.task_b = VoiceSessionManager(ttl_seconds=600, max_active=10, store=self.store, ticket_ttl_seconds=30)

    def tearDown(self) -> None:
        self.store.close()

    def test_a_ticket_opened_on_one_task_works_once_on_another(self) -> None:
        session, ticket = self.task_a.create_access(_person())
        claimed = self.task_b.claim_transport(session.session_id, ticket)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.principal_id, "stu-1")
        self.assertIn(Capability.AGENT_COMMAND, claimed.capabilities)
        self.assertEqual(self.task_b.get(session.session_id).institution_scope, InstitutionScope("college_a"))
        self.assertIsNone(self.task_a.claim_transport(session.session_id, ticket), "a replayed ticket opens nothing")

    def test_a_wrong_or_expired_ticket_is_refused(self) -> None:
        session, ticket = self.task_a.create_access(_person())
        self.assertIsNone(self.task_b.claim_transport(session.session_id, ticket + "x"))
        self.assertIsNone(self.task_b.claim_transport(session.session_id, ""))
        later = datetime.now(timezone.utc) + timedelta(seconds=31)
        with mock.patch.object(VoiceSessionManager, "_now", staticmethod(lambda: later)):
            self.assertIsNone(self.task_b.claim_transport(session.session_id, ticket))

    def test_closing_on_one_task_is_seen_by_the_other(self) -> None:
        session, ticket = self.task_a.create_access(_person())
        self.assertTrue(self.task_b.close(session.session_id, "stu-1"))
        self.assertIsNone(self.task_a.get(session.session_id))
        self.assertIsNone(self.task_a.claim_transport(session.session_id, ticket))
        self.assertFalse(self.task_a.close(session.session_id, "stu-1"))

    def test_only_the_owner_can_close_a_session(self) -> None:
        session, _ = self.task_a.create_access(_person())
        self.assertFalse(self.task_b.close(session.session_id, "someone-else"))
        self.assertIsNotNone(self.task_b.get(session.session_id))

    def test_a_released_socket_ends_its_session(self) -> None:
        session, ticket = self.task_a.create_access(_person())
        self.task_b.claim_transport(session.session_id, ticket)
        self.task_b.release_transport(session.session_id)
        self.assertIsNone(self.task_a.get(session.session_id))

    def test_opening_again_replaces_the_oldest_of_a_persons_sessions(self) -> None:
        manager = VoiceSessionManager(ttl_seconds=600, max_active=10, store=self.store, max_per_person=2)
        first = manager.create(_person())
        second = manager.create(_person())
        third = manager.create(_person())
        self.assertIsNone(manager.get(first.session_id))
        self.assertIsNotNone(manager.get(second.session_id))
        self.assertIsNotNone(manager.get(third.session_id))
        self.assertEqual(len(manager.active()), 2)

    def test_the_platform_wide_limit_holds_across_tasks(self) -> None:
        a = VoiceSessionManager(ttl_seconds=600, max_active=2, store=self.store)
        b = VoiceSessionManager(ttl_seconds=600, max_active=2, store=self.store)
        a.create(_person("p1"))
        b.create(_person("p2"))
        with self.assertRaises(RuntimeError):
            a.create(_person("p3"))

    def test_an_unclaimed_ticket_stops_counting_once_it_lapses(self) -> None:
        manager = VoiceSessionManager(ttl_seconds=600, max_active=1, store=self.store, ticket_ttl_seconds=10)
        manager.create(_person("p1"))
        later = datetime.now(timezone.utc) + timedelta(seconds=11)
        with mock.patch.object(VoiceSessionManager, "_now", staticmethod(lambda: later)):
            manager.create(_person("p2"))

    def test_scope_and_capability_are_enforced(self) -> None:
        with self.assertRaises(VoiceScopeError):
            self.task_a.create_access(_person(), InstitutionScope("college_b"))
        mute = Principal("p", PrincipalType.STUDENT, frozenset({Capability.ASK_READ_ONLY}), (InstitutionScope("college_a"),), True)
        with self.assertRaises(PermissionError):
            self.task_a.create(mute)


class UsageCounterTests(unittest.TestCase):
    def _check(self, store) -> None:
        taken = [store.take_usage(institution_id="c", principal_id="p", counter="web_search", day="2026-09-23", cap=2) for _ in range(3)]
        self.assertEqual(taken, [True, True, False])
        self.assertTrue(store.take_usage(institution_id="c", principal_id="p", counter="web_search", day="2026-09-24", cap=2))
        self.assertTrue(store.take_usage(institution_id="c", principal_id="q", counter="web_search", day="2026-09-23", cap=2))
        self.assertFalse(store.take_usage(institution_id="c", principal_id="r", counter="web_search", day="2026-09-23", cap=0))

    def test_sqlite_counts_against_the_cap(self) -> None:
        store = SqliteControlStore(":memory:")
        self._check(store)
        store.close()

    def test_memory_counts_against_the_cap(self) -> None:
        self._check(InMemoryControlStore())

    def test_pruning_drops_old_counters_and_finished_sessions(self) -> None:
        for store in (SqliteControlStore(":memory:"), InMemoryControlStore()):
            store.take_usage(institution_id="c", principal_id="p", counter="web_search", day="2020-01-01", cap=5)
            manager = VoiceSessionManager(store=store)
            session = manager.create(_person())
            manager.close(session.session_id, "stu-1")
            self.assertEqual(store.prune_ephemeral(datetime.now(timezone.utc)), 1, "only the old counter; the closed session is kept a day")
            self.assertEqual(store.prune_ephemeral(datetime.now(timezone.utc) + timedelta(days=2)), 1)
            self.assertIsNone(store.get_voice_session(session.session_id))
            store.close()

    def test_timestamps_compare_as_text(self) -> None:
        base = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
        self.assertLess(timestamp(base), timestamp(base + timedelta(microseconds=1)))
        self.assertLess(timestamp(base + timedelta(seconds=1)), timestamp(base + timedelta(seconds=10)))


if __name__ == "__main__":
    unittest.main()
