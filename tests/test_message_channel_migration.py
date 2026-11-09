"""The #201 migration: the best-effort channel of messages that already exist, down and up on seeded data, in a scratch
database of its own."""

from tests.test_approval_groups_migration import rows, scratch  # noqa: F401  (scratch is a fixture)

BEFORE = "cbf4e82d802a"

SEED = """
INSERT INTO "user" (id, email, display_name, role, created_at) OVERRIDING SYSTEM VALUE VALUES (1,'agent@x.com','Agent','AGENT',now());
INSERT INTO ticket (id, number, subject, type, source, status, requester_email, needs_category_review, is_escalated, reported_at, created_at, updated_at)
 VALUES
 (1,201,'by mail','INCIDENT','EMAIL','OPEN','r@x.com',false,false,now(),now(),now()),
 (2,202,'by portal','INCIDENT','PORTAL','OPEN','r@x.com',false,false,now(),now(),now()),
 (3,203,'by teams','INCIDENT','TEAMS','OPEN','r@x.com',false,false,now(),now(),now()),
 (4,204,'by an agent','INCIDENT','AGENT','OPEN','r@x.com',false,false,now(),now(),now());
INSERT INTO message (id, ticket_id, sender, body, direction, visibility, msp_forward, graph_message_id, author_user_id, created_at) VALUES
 (1,1,'r@x.com','mail in','INBOUND','PUBLIC',NULL,'g1',NULL,now()),
 (2,2,'R','portal in','INBOUND','PUBLIC',NULL,NULL,NULL,now()),
 (3,3,'R','teams in','INBOUND','PUBLIC',NULL,NULL,NULL,now()),
 (4,4,'Logged by Agent','by agent','INBOUND','PUBLIC',NULL,NULL,NULL,now()),
 (5,1,'Agent','note','INTERNAL','AGENTS_ONLY',NULL,NULL,1,now()),
 (6,1,'Agent','reply by email','OUTBOUND','PUBLIC',NULL,NULL,1,now()),
 (7,3,'Agent','reply to teams','OUTBOUND','PUBLIC',NULL,NULL,1,now()),
 (8,1,'IT Support (automatic)','notice','OUTBOUND','PUBLIC',NULL,NULL,NULL,now()),
 (9,1,'msp@x.com','from the msp','OUTBOUND','PUBLIC','forwarded','g9',NULL,now());
INSERT INTO teamspost (kind, ticket_id, message_id, attempts, created_at) VALUES ('requester_message',3,7,0,now());
"""


def test_existing_messages_get_the_channel_that_can_be_worked_out_and_the_rest_stay_null(scratch):
    alembic, connect = scratch
    alembic("upgrade", BEFORE)
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(SEED)

    alembic("upgrade", "head")

    got = dict(rows(connect, "SELECT id, channel FROM message ORDER BY id"))
    assert got == {1: "email", 2: "portal", 3: "teams", 4: None, 5: "agent", 6: "email", 7: "teams", 8: "system", 9: "email"}

    alembic("downgrade", BEFORE)
    assert rows(connect, "SELECT count(*) FROM message") == [(9,)]
    alembic("upgrade", "head")
    alembic("check")
