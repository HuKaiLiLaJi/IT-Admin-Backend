import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import SCHEMA, SCHEMA_VERSION, TICKET_SCHEMA, connect, initialize_database
from tests.test_tickets import TicketHTTPTestCase


class TicketActionTests(TicketHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.submitter = self.login()
        self.dispatcher = self.login("dispatcher")
        self.handler = self.login("handler")
        self.other_handler = self.login("handler_backup")

    def act(self, ticket, action, token, **fields):
        return self.request(
            f"/api/v1/tickets/{ticket['id']}/{action}", "POST",
            {"expected_version": ticket["version"], **fields}, token,
        )

    def assigned(self):
        ticket = self.ticket(self.submitter)
        status, ticket, _ = self.act(ticket, "assignments", self.dispatcher, assignee_id="test-handler")
        self.assertEqual(status, 200)
        return ticket

    def detail(self, ticket, token=None):
        status, detail, _ = self.request(
            f"/api/v1/tickets/{ticket['id']}", token=token or self.submitter,
        )
        self.assertEqual(status, 200)
        return detail

    def test_assignment_accept_note_reassign_and_restart(self):
        status, choices, _ = self.request("/api/v1/support-groups/test-it/handlers", token=self.dispatcher)
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in choices["items"]}, {"test-handler", "test-handler_backup"})
        ticket = self.assigned()
        self.assertEqual(ticket["status"], "pending_acceptance")
        self.assertIsInstance(ticket["assigned_at"], int)
        self.assertIsNone(ticket["accepted_at"])
        status, ticket, _ = self.act(ticket, "accept", self.handler)
        self.assertEqual(status, 200)
        self.assertEqual(ticket["status"], "in_progress")
        self.assertGreaterEqual(ticket["accepted_at"], ticket["assigned_at"])
        status, ticket, _ = self.act(ticket, "notes", self.handler, note=" 已检查线路 ")
        self.assertEqual(status, 200)
        status, ticket, _ = self.act(
            ticket, "reassignments", self.dispatcher, assignee_id="test-handler_backup", reason=" 转交同组人员 ",
        )
        self.assertEqual(status, 200)
        self.assertEqual(ticket["status"], "pending_acceptance")
        self.assertIsNone(ticket["accepted_at"])
        self.assertEqual(self.request(f"/api/v1/tickets/{ticket['id']}", token=self.handler)[0], 404)
        self.assertEqual(self.act(ticket, "accept", self.handler)[0], 404)
        status, ticket, _ = self.act(ticket, "accept", self.other_handler)
        self.assertEqual(status, 200)
        self.stop_server()
        self.start_server()
        detail = self.detail(ticket)
        self.assertEqual(detail["version"], 6)
        self.assertEqual([event["action"] for event in detail["history"]],
                         ["created", "assigned", "accepted", "note_added", "reassigned", "accepted"])
        self.assertEqual(detail["history"][3]["note"], "已检查线路")
        event = detail["history"][4]
        self.assertEqual(event["actor_id"], "test-dispatcher")
        self.assertEqual(event["from_assignee_id"], "test-handler")
        self.assertEqual(event["to_assignee_id"], "test-handler_backup")
        self.assertEqual(event["note"], "转交同组人员")
        self.assertEqual(event["from_status"], "in_progress")
        self.assertEqual(event["to_status"], "pending_acceptance")
        self.assertEqual(self.detail(ticket, self.other_handler), detail)

    def test_permissions_and_invalid_targets(self):
        ticket = self.ticket(self.submitter)
        for action, fields in (
            ("assignments", {"assignee_id": "test-handler"}),
            ("reassignments", {"assignee_id": "test-handler_backup", "reason": "交接"}),
            ("accept", {}), ("notes", {"note": "进展"}),
        ):
            self.assertEqual(self.act(ticket, action, None, **fields)[0], 401)
            self.assertEqual(self.act(ticket, action, self.submitter, **fields)[0], 403)
        for target in ("missing", "test-submitter", "test-dispatcher", "test-handler_other"):
            self.assertEqual(self.act(ticket, "assignments", self.dispatcher, assignee_id=target)[0], 422)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE users SET active = 0 WHERE id = 'test-handler_backup'")
        self.assertEqual(self.act(ticket, "assignments", self.dispatcher, assignee_id="test-handler_backup")[0], 422)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-handler_backup' AND group_id = 'test-it'")
            connection.execute("UPDATE users SET active = 1 WHERE id = 'test-handler_backup'")
        self.assertEqual(self.act(ticket, "assignments", self.dispatcher, assignee_id="test-handler_backup")[0], 422)
        other_group = self.ticket(self.submitter, "test-facility")
        self.assertEqual(self.act(other_group, "assignments", self.dispatcher, assignee_id="test-handler_other")[0], 404)
        self.assertEqual(self.request("/api/v1/support-groups/test-admin/handlers", token=self.dispatcher)[0], 403)
        self.assertEqual(self.request("/api/v1/support-groups/test-it/handlers", token=self.submitter)[0], 403)
        self.assertEqual(len(self.detail(ticket)["history"]), 1)

    def test_state_validation_versions_and_pending_reassignment(self):
        ticket = self.assigned()
        self.assertEqual(self.act(ticket, "notes", self.handler, note="提前备注")[0], 409)
        self.assertEqual(self.act(ticket, "assignments", self.dispatcher, assignee_id="test-handler")[0], 409)
        for body in ({}, {"expected_version": 0}, {"expected_version": True},
                     {"expected_version": "2"}, {"expected_version": 2, "status": "closed"}):
            self.assertEqual(self.request(f"/api/v1/tickets/{ticket['id']}/accept", "POST", body, self.handler)[0], 422)
        self.assertEqual(self.act(ticket, "reassignments", self.dispatcher,
                                  assignee_id="test-handler", reason="相同处理人")[0], 422)
        self.assertEqual(self.act(ticket, "reassignments", self.dispatcher,
                                  assignee_id="test-handler_backup", reason=" \n ")[0], 422)
        status, updated, _ = self.act(ticket, "reassignments", self.dispatcher,
                                      assignee_id="test-handler_backup", reason="待接单交接")
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "pending_acceptance")
        stale = self.act(ticket, "reassignments", self.dispatcher, assignee_id="test-handler", reason="过时请求")
        self.assertEqual(stale[0], 409)
        self.assertEqual(stale[1]["detail"]["code"], "version_conflict")
        status, active, _ = self.act(updated, "accept", self.other_handler)
        self.assertEqual(status, 200)
        self.assertEqual(self.act(updated, "accept", self.other_handler)[0], 409)
        self.assertEqual(self.act(active, "accept", self.other_handler)[0], 409)
        self.assertEqual(self.act(active, "notes", self.other_handler, note=" " )[0], 422)
        for state in ("awaiting_confirmation", "closed", "cancelled"):
            with connect(self.path) as connection, connection:
                connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (state, active["id"]))
            self.assertEqual(self.act(active, "reassignments", self.dispatcher,
                                      assignee_id="test-handler", reason="禁止的状态夹具")[0], 409)
            self.assertEqual(self.act(active, "notes", self.other_handler, note="进展")[0], 409)

    def test_concurrent_accept_and_accept_reassign_conflicts(self):
        for scenario in ("accept", "mixed", "reassign"):
            ticket = self.assigned()
            barrier = threading.Barrier(2)

            def perform(index):
                barrier.wait(timeout=10)
                if scenario == "reassign" or (scenario == "mixed" and index == 1):
                    return self.act(ticket, "reassignments", self.dispatcher,
                                    assignee_id="test-handler_backup", reason="并发改派")
                return self.act(ticket, "accept", self.handler)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(perform, range(2)))
            self.assertEqual(sum(result[0] == 200 for result in results), 1)
            # Losing access after reassignment is reported as the usual resource 404.
            self.assertTrue(all(result[0] in (200, 409, 404) for result in results))
            detail = self.detail(ticket)
            self.assertEqual(detail["version"], 3)
            self.assertEqual(len(detail["history"]), 3)
            self.assertEqual(detail["status"], detail["history"][-1]["to_status"])
            self.assertEqual(detail["assignee_id"], detail["history"][-1]["to_assignee_id"])

    def test_failed_history_rolls_back_every_action(self):
        ticket = self.ticket(self.submitter)
        actions = (
            ("assignments", self.dispatcher, {"assignee_id": "test-handler"}),
            ("accept", self.handler, {}),
            ("notes", self.handler, {"note": "检查记录"}),
            ("reassignments", self.dispatcher, {"assignee_id": "test-handler_backup", "reason": "交接"}),
        )
        for action, token, fields in actions:
            before = self.detail(ticket)
            with connect(self.path) as connection, connection:
                connection.execute("""CREATE TRIGGER fail_action BEFORE INSERT ON ticket_history
                                      BEGIN SELECT unavailable_test_function(); END""")
            self.assertEqual(self.act(ticket, action, token, **fields)[0], 503)
            self.assertEqual(self.detail(ticket), before)
            with connect(self.path) as connection, connection:
                connection.execute("DROP TRIGGER fail_action")
            status, ticket, _ = self.act(ticket, action, token, **fields)
            self.assertEqual(status, 200)

    def test_role_and_scope_revocation(self):
        ticket = self.assigned()
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-dispatcher'")
        self.assertEqual(self.act(ticket, "reassignments", self.dispatcher,
                                  assignee_id="test-handler_backup", reason="权限已收回")[0], 404)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-handler'")
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-handler' AND role = 'handler'")
        self.assertEqual(self.act(ticket, "accept", self.handler)[0], 404)
        self.assertEqual(len(self.detail(ticket)["history"]), 2)

    def test_v2_migration_preserves_tickets_and_is_atomic(self):
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        with connect(legacy) as connection, connection:
            for statement in (*SCHEMA, *TICKET_SCHEMA):
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 2")
        # Copy a real T02-compatible ticket and initial history into a v2 schema.
        ticket = self.ticket(self.submitter)
        with connect(self.path) as source, connect(legacy) as destination, destination:
            for table in ("users", "roles", "user_roles", "support_groups", "user_group_scopes",
                          "service_systems", "conversations", "tickets", "ticket_history"):
                columns = [row[1] for row in destination.execute(f"PRAGMA table_info({table})")]
                names = ", ".join(columns)
                placeholders = ", ".join("?" for _ in columns)
                destination.executemany(f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                                        source.execute(f"SELECT {names} FROM {table}").fetchall())
        initialize_database(legacy)
        initialize_database(legacy)
        with connect(legacy) as connection:
            row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket["id"],)).fetchone()
            for field in row.keys():
                self.assertEqual(row[field], ticket[field])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_history").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        broken = Path(self.directory.name) / "broken.sqlite3"
        with connect(broken) as connection, connection:
            for statement in (*SCHEMA, *TICKET_SCHEMA):
                connection.execute(statement)
            connection.execute("ALTER TABLE tickets ADD COLUMN accepted_at INTEGER")
            connection.execute("PRAGMA user_version = 2")
        with self.assertRaises(sqlite3.OperationalError):
            initialize_database(broken)
        with connect(broken) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertNotIn("assigned_at", [row[1] for row in connection.execute("PRAGMA table_info(tickets)")])
