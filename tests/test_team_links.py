"""Team visibility links: access beyond the ticket's own team comes only from an
admin-set link, one hop, capped by the agent's own level."""

from sqlmodel import select

from app.models import AuditLog, MembershipLevel, TeamLink, Ticket, TicketStatus, User
from app.visibility import Access, access_context, ticket_access, visible_ticket_condition
from tests.conftest import create_team, login_as
from tests.test_visibility import agent_in, make_ticket


def link(db_session, from_team, to_team, level=MembershipLevel.VIEWER):
    db_session.add(TeamLink(from_team_id=from_team.id, to_team_id=to_team.id, level=level))
    db_session.commit()


def test_no_link_gives_nothing_extra(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, member_of=[a])
    ticket = make_ticket(db_session, b.id)
    assert client.get(f"/tickets/{ticket.number}").status_code == 403


def test_a_view_link_reads_but_cannot_change(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, member_of=[a])
    link(db_session, a, b, MembershipLevel.VIEWER)
    ticket = make_ticket(db_session, b.id, subject="Over there")

    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert "Over there" in client.get("/tickets").text
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"}).status_code == 403


def test_a_work_link_works_the_ticket(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, member_of=[a])
    link(db_session, a, b, MembershipLevel.MEMBER)
    ticket = make_ticket(db_session, b.id)
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"}).status_code == 200


def test_links_are_one_way(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, member_of=[b])
    link(db_session, a, b, MembershipLevel.MEMBER)  # A sees B, not the reverse
    ticket = make_ticket(db_session, a.id)
    assert client.get(f"/tickets/{ticket.number}").status_code == 403


def test_links_do_not_chain(client, db_session):
    a, b, c = (create_team(db_session, name) for name in "ABC")
    agent_in(client, db_session, member_of=[a])
    link(db_session, a, b, MembershipLevel.MEMBER)
    link(db_session, b, c, MembershipLevel.MEMBER)
    assert client.get(f"/tickets/{make_ticket(db_session, b.id).number}").status_code == 200
    assert client.get(f"/tickets/{make_ticket(db_session, c.id).number}").status_code == 403


def test_a_viewer_of_the_linking_team_never_gets_more_than_view(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, viewer_of=[a])
    link(db_session, a, b, MembershipLevel.MEMBER)
    ticket = make_ticket(db_session, b.id)
    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"}).status_code == 403


def test_a_link_only_helps_agents_of_the_linking_team(client, db_session):
    a, b, c = (create_team(db_session, name) for name in "ABC")
    agent_in(client, db_session, member_of=[c])
    link(db_session, a, b, MembershipLevel.MEMBER)
    assert client.get(f"/tickets/{make_ticket(db_session, b.id).number}").status_code == 403


def test_removing_a_link_takes_access_away_at_once(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, member_of=[a])
    link(db_session, a, b, MembershipLevel.MEMBER)
    ticket = make_ticket(db_session, b.id)
    assert client.get(f"/tickets/{ticket.number}").status_code == 200

    db_session.delete(db_session.get(TeamLink, (a.id, b.id)))
    db_session.commit()
    assert client.get(f"/tickets/{ticket.number}").status_code == 403


def test_list_query_and_single_ticket_rule_agree_with_links(client, db_session):
    own, watched, linked, via_viewer, chained, nobody = (
        create_team(db_session, name) for name in ("Own", "Watched", "Linked", "ViaViewer", "Chained", "None")
    )
    agent_id = agent_in(client, db_session, member_of=[own], viewer_of=[watched])
    link(db_session, own, linked, MembershipLevel.MEMBER)
    link(db_session, watched, via_viewer, MembershipLevel.MEMBER)
    link(db_session, linked, chained, MembershipLevel.MEMBER)
    for team in (own, watched, linked, via_viewer, chained, nobody):
        make_ticket(db_session, team.id, TicketStatus.OPEN)
        make_ticket(db_session, team.id, TicketStatus.NEW)

    agent = db_session.get(User, agent_id)
    levels = access_context(db_session, agent)
    tickets = db_session.exec(select(Ticket)).all()
    by_rule = {t.id for t in tickets if ticket_access(agent, t, levels) >= Access.READ}
    by_query = {t.id for t in db_session.exec(select(Ticket).where(visible_ticket_condition(agent, levels))).all()}
    assert by_rule == by_query

    def triaged_in(team):
        return next(t for t in tickets if t.team_id == team.id and t.triaged_at)

    assert ticket_access(agent, triaged_in(linked), levels) == Access.WORK
    assert ticket_access(agent, triaged_in(via_viewer), levels) == Access.READ
    assert ticket_access(agent, triaged_in(chained), levels) == Access.NONE
    assert ticket_access(agent, triaged_in(nobody), levels) == Access.NONE


# ---- Admin ------------------------------------------------------------------


def test_admin_manages_links_with_audit(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    login_as(client, "admin@example.com")

    response = client.post(
        "/admin/team-links", data={"from_team_id": a.id, "to_team_id": b.id, "level": "viewer"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert db_session.get(TeamLink, (a.id, b.id)).level == MembershipLevel.VIEWER
    client.post("/admin/team-links", data={"from_team_id": a.id, "to_team_id": b.id, "level": "member"})
    db_session.expire_all()
    assert db_session.get(TeamLink, (a.id, b.id)).level == MembershipLevel.MEMBER
    assert client.get("/admin/teams").status_code == 200

    client.post(f"/admin/team-links/{a.id}/{b.id}/remove")
    db_session.expire_all()
    assert db_session.get(TeamLink, (a.id, b.id)) is None

    rows = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "team").order_by(AuditLog.id)).all()
    assert [row.action for row in rows] == ["link_added", "link_changed", "link_removed"]


def test_admin_rejects_self_links_and_unknown_teams(client, db_session):
    a = create_team(db_session, "A")
    login_as(client, "admin@example.com")
    assert client.post("/admin/team-links", data={"from_team_id": a.id, "to_team_id": a.id, "level": "viewer"}).status_code == 400
    assert client.post("/admin/team-links", data={"from_team_id": a.id, "to_team_id": 999, "level": "viewer"}).status_code == 404
    assert client.post("/admin/team-links/1/2/remove").status_code == 404


def test_only_super_admins_manage_links(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, member_of=[a])
    assert client.post("/admin/team-links", data={"from_team_id": a.id, "to_team_id": b.id, "level": "viewer"}).status_code == 403
    assert db_session.exec(select(TeamLink)).all() == []
