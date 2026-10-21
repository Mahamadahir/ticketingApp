"""The 4C-1 data migration, down and up on seeded data, in a scratch database of its own (the test
database is rebuilt from the models for every test, so it cannot be migrated)."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parent.parent
BEFORE = "b4c6d8e0f2a6"  # the head before approval groups
AFTER = "c5d9e2f7a418"

SEED = """
INSERT INTO "user" (id, email, display_name, role, created_at) OVERRIDING SYSTEM VALUE VALUES
 (1,'a@x.com','A','END_USER',now()),(2,'b@x.com','B','END_USER',now()),(3,'c@x.com','C','END_USER',now()),(4,'req@x.com','Req','END_USER',now());
SELECT setval(pg_get_serial_sequence('"user"','id'), 10);
INSERT INTO requesttype (id, name, description, is_active, approvals_required, declines_required, created_at) VALUES
 (1,'New starter','',true,2,1,now()),(2,'Software','',true,1,1,now()),(3,'Empty','',false,1,1,now()),(4,'Unanimous','',true,3,3,now());
INSERT INTO requesttypeapprover (request_type_id, user_id) VALUES (1,1),(1,2),(1,3),(2,1),(2,2),(4,1),(4,2),(4,3);
INSERT INTO ticket (id, number, subject, type, source, status, requester_email, request_type_id, approvals_required, declines_required,
  needs_category_review, is_escalated, reported_at, created_at, updated_at) VALUES
 (1,101,'pending one','SERVICE_REQUEST','PORTAL','AWAITING_APPROVAL','req@x.com',1,2,1,false,false,now(),now(),now()),
 (2,102,'approved one','SERVICE_REQUEST','PORTAL','OPEN','req@x.com',2,1,1,false,false,now(),now(),now()),
 (3,103,'declined one','SERVICE_REQUEST','PORTAL','CLOSED','req@x.com',1,2,1,false,false,now(),now(),now()),
 (4,104,'waived one','SERVICE_REQUEST','PORTAL','OPEN','req@x.com',1,2,1,false,false,now(),now(),now());
INSERT INTO ticketevent (ticket_id, event_type, actor_email, to_value, created_at) VALUES
 (2,'approval_outcome','a@x.com','approved',now()),(3,'approval_outcome','b@x.com','declined',now()),(4,'approval_outcome','admin@x.com','waived',now());
INSERT INTO ticketapproval (id, ticket_id, approver_id, decision, comment, created_at, decided_at) VALUES
 (1,1,1,'approved','ok',now(),now()),(2,1,2,'pending','',now(),NULL),(3,1,3,'pending','',now(),NULL),
 (4,2,1,'approved','fine',now(),now()),(5,2,2,'not_needed','',now(),now()),
 (6,3,1,'declined','no',now(),now()),(7,3,2,'declined','no',now(),now()),(8,3,3,'not_needed','',now(),now()),
 (9,4,1,'not_needed','',now(),now()),(10,4,2,'not_needed','',now(),now()),(11,4,3,'not_needed','',now(),now());
INSERT INTO outboundemail (id, kind, ticket_id, approval_id, to_address, created_at) VALUES
 (1,'approval',1,2,'b@x.com',now()),(2,'approval',1,3,'c@x.com',now());
SELECT setval(pg_get_serial_sequence('ticketapproval','id'), 11);
"""


@pytest.fixture
def scratch():
    """An empty scratch database (the test database's name plus _mig) and a runner for alembic in it."""
    parsed = make_url(TEST_DATABASE_URL)
    name = f"{parsed.database}_mig"
    admin = psycopg2.connect(host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cursor.execute(f'CREATE DATABASE "{name}"')
    url = parsed.set(database=name).render_as_string(hide_password=False)

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url, "ENV": "dev"},
            capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, result.stderr
        return result

    def connect():
        connection = psycopg2.connect(host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname=name)
        connection.autocommit = True
        return connection

    yield alembic, connect
    with admin.cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin.close()


def rows(connect, query):
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchall()


def test_the_data_migrates_up_and_back_down(scratch):
    alembic, connect = scratch
    alembic("upgrade", BEFORE)
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(SEED)

    alembic("upgrade", AFTER)

    # Each type's approvers became a group named after it.
    assert rows(connect, "SELECT g.name, array_agg(u.email ORDER BY u.email) FROM approvalgroup g "
                         "JOIN approvalgroupmember m ON m.group_id = g.id JOIN \"user\" u ON u.id = m.user_id "
                         "GROUP BY g.name ORDER BY g.name") == [
        ("New starter", ["a@x.com", "b@x.com", "c@x.com"]),
        ("Software", ["a@x.com", "b@x.com"]),
        ("Unanimous", ["a@x.com", "b@x.com", "c@x.com"]),
    ]
    assert rows(connect, "SELECT name FROM approvalgroup ORDER BY name") == [("Empty",), ("New starter",), ("Software",), ("Unanimous",)]
    # 2 approve / 1 decline of 3 -> 66% / 33%, raised to 35% so the two add up to more than 100;
    # 1 / 1 of 2 -> 50% / 50%, raised to 51%; 3 / 3 of 3 -> 100%, and one decline already made the approvals
    # unreachable, so 33%; no approvers keeps the defaults.
    assert rows(connect, "SELECT t.name, g.name, t.pass_percent, t.fail_percent FROM requesttype t "
                         "JOIN approvalgroup g ON g.id = t.approval_group_id ORDER BY t.id") == [
        ("New starter", "New starter", 66, 35),
        ("Software", "Software", 50, 51),
        ("Empty", "Empty", 51, 50),
        ("Unanimous", "Unanimous", 100, 33),
    ]
    # One approval per ticket, copying the rule and fixing the voters; the status follows the old state.
    assert rows(connect, "SELECT ticket_id, g.name, voters, pass_percent, fail_percent, a.status, settled_at IS NULL "
                         "FROM ticketapproval a JOIN approvalgroup g ON g.id = a.group_id ORDER BY ticket_id") == [
        (1, "New starter", 3, 66, 35, "pending", True),
        (2, "Software", 2, 50, 51, "approved", False),
        (3, "New starter", 3, 66, 35, "declined", False),
        (4, "New starter", 3, 66, 35, "waived", False),
    ]
    # Votes keep the old ids and decisions, so emailed links and queued mail still point at the same person.
    assert rows(connect, "SELECT v.id, a.ticket_id, v.voter_id, v.decision, v.comment FROM approvalvote v "
                         "JOIN ticketapproval a ON a.id = v.approval_id ORDER BY v.id") == [
        (1, 1, 1, "approved", "ok"), (2, 1, 2, "pending", ""), (3, 1, 3, "pending", ""),
        (4, 2, 1, "approved", "fine"), (5, 2, 2, "not_needed", ""),
        (6, 3, 1, "declined", "no"), (7, 3, 2, "declined", "no"), (8, 3, 3, "not_needed", ""),
        (9, 4, 1, "not_needed", ""), (10, 4, 2, "not_needed", ""), (11, 4, 3, "not_needed", ""),
    ]
    assert rows(connect, "SELECT id, vote_id FROM outboundemail ORDER BY id") == [(1, 2), (2, 3)]
    # New votes get fresh ids (the sequence was moved past the copied ones).
    assert rows(connect, "SELECT nextval(pg_get_serial_sequence('approvalvote', 'id'))") == [(12,)]
    assert rows(connect, "SELECT to_regclass('requesttypeapprover'), to_regclass('approvalgroup')")[0][0] is None

    alembic("downgrade", "-1")

    assert rows(connect, "SELECT id, approvals_required, declines_required FROM requesttype ORDER BY id") == [
        (1, 2, 2), (2, 1, 2), (3, 1, 1), (4, 3, 1),
    ]
    assert rows(connect, "SELECT request_type_id, user_id FROM requesttypeapprover ORDER BY request_type_id, user_id") == [
        (1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (4, 1), (4, 2), (4, 3),
    ]
    assert rows(connect, "SELECT id, approvals_required, declines_required FROM ticket ORDER BY id") == [
        (1, 2, 2), (2, 1, 2), (3, 2, 2), (4, 2, 2),
    ]
    assert rows(connect, "SELECT id, ticket_id, approver_id, decision FROM ticketapproval ORDER BY id") == [
        (1, 1, 1, "approved"), (2, 1, 2, "pending"), (3, 1, 3, "pending"),
        (4, 2, 1, "approved"), (5, 2, 2, "not_needed"),
        (6, 3, 1, "declined"), (7, 3, 2, "declined"), (8, 3, 3, "not_needed"),
        (9, 4, 1, "not_needed"), (10, 4, 2, "not_needed"), (11, 4, 3, "not_needed"),
    ]
    assert rows(connect, "SELECT id, approval_id FROM outboundemail ORDER BY id") == [(1, 2), (2, 3)]
    assert rows(connect, "SELECT to_regclass('approvalvote'), to_regclass('approvalgroup')") == [(None, None)]

    # The way down is best effort (a percentage rounds to a whole number of votes): what matters is that
    # the data survives and the way up works again from it.
    alembic("upgrade", AFTER)

    assert rows(connect, "SELECT ticket_id, voters, pass_percent, fail_percent, status FROM ticketapproval ORDER BY ticket_id") == [
        (1, 3, 66, 66, "pending"), (2, 2, 50, 100, "approved"), (3, 3, 66, 66, "declined"), (4, 3, 66, 66, "waived"),
    ]
    assert rows(connect, "SELECT count(*) FROM approvalvote") == [(11,)]
    assert rows(connect, "SELECT id, vote_id FROM outboundemail ORDER BY id") == [(1, 2), (2, 3)]
    alembic("check")


def test_an_empty_database_migrates_both_ways(scratch):
    alembic, connect = scratch
    alembic("upgrade", "head")
    alembic("check")
    alembic("downgrade", "-1")
    alembic("upgrade", "head")
    alembic("check")
    assert alembic("heads").stdout.count("(head)") == 1


def test_approvals_of_a_ticket_retyped_away_from_its_request_type_are_kept(scratch):
    alembic, connect = scratch
    alembic("upgrade", BEFORE)
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("""
INSERT INTO "user" (id, email, display_name, role, created_at) OVERRIDING SYSTEM VALUE VALUES
 (1,'a@x.com','A','END_USER',now()),(2,'b@x.com','B','END_USER',now());
INSERT INTO ticket (id, number, subject, type, source, status, requester_email, request_type_id, approvals_required, declines_required,
  needs_category_review, is_escalated, reported_at, created_at, updated_at) VALUES
 (5,105,'retyped','INCIDENT','PORTAL','OPEN','req@x.com',NULL,1,1,false,false,now(),now(),now());
INSERT INTO ticketapproval (id, ticket_id, approver_id, decision, comment, created_at, decided_at) VALUES
 (12,5,1,'approved','fine',now(),now()),(13,5,2,'not_needed','',now(),now());
""")

    alembic("upgrade", AFTER)

    assert rows(connect, "SELECT v.id, v.voter_id, v.decision, v.comment, a.voters FROM approvalvote v "
                         "JOIN ticketapproval a ON a.id = v.approval_id ORDER BY v.id") == [
        (12, 1, "approved", "fine", 2), (13, 2, "not_needed", "", 2),
    ]
    assert rows(connect, "SELECT count(*) FROM approvalvote") == [(2,)]
    assert rows(connect, "SELECT count(*) FROM approvalgroupmember") == [(2,)]
