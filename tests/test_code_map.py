"""The code map generator (tools/code_map.py): deterministic, finds routes and models, flags seam
violations, and --check fails on a stale map. These tests need no database."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("code_map", ROOT / "tools" / "code_map.py")
code_map = importlib.util.module_from_spec(_spec)
sys.modules["code_map"] = code_map
_spec.loader.exec_module(code_map)


@pytest.fixture(autouse=True)
def fresh_database():
    """Overrides the conftest fixture of the same name: nothing here touches Postgres."""


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Same: no database, no rate limits to reset."""


def write(root: Path, relpath: str, text: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def fixture_repo(tmp_path: Path) -> Path:
    write(tmp_path, "app/__init__.py", "")
    write(
        tmp_path,
        "app/models.py",
        '"""Tables."""\nfrom sqlmodel import SQLModel\n\n'
        "class Ticket(SQLModel, table=True):\n    id: int\n\n"
        "class Message(SQLModel, table=True):\n    id: int\n",
    )
    write(
        tmp_path,
        "app/messages.py",
        '"""The messages seam."""\nfrom sqlmodel import select\nfrom app.models import Message\n\n'
        "def visible(session):\n    return select(Message)\n",
    )
    write(tmp_path, "app/raising.py", '"""Raising."""\nfrom app.models import Ticket\n')
    write(tmp_path, "app/visibility.py", '"""Visibility."""\ndef visible_ticket_condition(user):\n    return None\n')
    write(tmp_path, "app/routers/__init__.py", "")
    write(
        tmp_path,
        "app/routers/bad.py",
        '"""A router that breaks the rules."""\n'
        "from fastapi import APIRouter\nfrom sqlmodel import select\nfrom app.models import Ticket, Message\n\n"
        'router = APIRouter(prefix="/bad")\n\n'
        '@router.get("/list")\ndef listing(session, ticket):\n'
        "    rows = session.exec(select(Ticket)).all()\n    return ticket.messages\n\n"
        '@router.post("/new")\ndef make(session):\n    return Ticket(source="agent")\n',
    )
    write(
        tmp_path,
        "app/routers/good.py",
        '"""A router that applies visibility."""\n'
        "from fastapi import APIRouter\nfrom sqlmodel import select\nfrom app.models import Ticket\n"
        "from app.visibility import visible_ticket_condition\n\n"
        'router = APIRouter(prefix="/good")\n\n'
        '@router.get("/")\ndef listing(session, user):\n    return select(Ticket).where(visible_ticket_condition(user))\n',
    )
    write(tmp_path, "app/other.py", '"""Imports a router."""\nfrom app.routers import good\n')
    write(tmp_path, "tests/test_good.py", "from app.routers import good\n")
    write(tmp_path, "migrations/versions/aaa_first.py", "revision: str = 'aaa'\ndown_revision = None\n")
    write(tmp_path, "migrations/versions/bbb_second.py", "revision = 'bbb'\ndown_revision = 'aaa'\n")
    return tmp_path


def test_output_is_deterministic(tmp_path):
    repo = fixture_repo(tmp_path)
    assert code_map.generate(repo) == code_map.generate(repo)
    real = code_map.generate(ROOT)
    assert real == code_map.generate(ROOT)
    assert "\r" not in real[code_map.MAP_MD] and "\r" not in real[code_map.MAP_JSON]


def test_finds_a_known_route_model_job_and_setting():
    data = json.loads(code_map.generate(ROOT)[code_map.MAP_JSON])
    modules = {m["module"]: m for m in data["modules"]}
    assert any(r["path"] == "/health" and r["method"] == "GET" for r in modules["app.routers.health"]["routes"])
    assert "ticket" in [t["table"] for t in modules["app.models"]["tables"]]
    assert "heartbeat" in [j["name"] for j in data["jobs"]]
    assert "DATABASE_URL" in [s["env"] for s in data["settings"]]
    assert len(data["migrations"]["heads"]) == 1  # one Alembic head
    assert "app.routers.health" in modules["app.main"]["imports"]
    assert "app.main" in modules["app.routers.health"]["imported_by"]


def test_secret_settings_show_no_default():
    data = json.loads(code_map.generate(ROOT)[code_map.MAP_JSON])
    secret = [s for s in data["settings"] if s["name"] == "session_secret_key"][0]
    assert secret["default"] == "(secret)"


def test_flags_seam_violations_in_a_fixture(tmp_path):
    repo = fixture_repo(tmp_path)
    data = json.loads(code_map.generate(repo)[code_map.MAP_JSON])
    found = {(v["rule"], v["file"]) for v in data["seams"]["violations"]}
    assert ("access-in-visibility", "app/routers/bad.py") in found
    assert ("messages-via-messages-module", "app/routers/bad.py") in found
    assert ("tickets-via-ticket-service", "app/routers/bad.py") in found
    assert ("routers-only-from-main", "app/other.py") in found
    # The router that applies a visibility helper, and the messages module itself, are clean.
    assert not [v for v in data["seams"]["violations"] if v["file"] in ("app/routers/good.py", "app/messages.py")]
    assert data["migrations"]["heads"][0]["revision"] == "bbb"
    assert [m for m in data["modules"] if m["module"] == "app.routers.good"][0]["tests"] == ["tests/test_good.py"]


def test_agent_source_outside_raising_is_flagged(tmp_path):
    repo = fixture_repo(tmp_path)
    body = '"""x"""\nfrom app.models import TicketSource\n\ndef f(make):\n    return make(source=TicketSource.AGENT)\n'
    write(repo, "app/sneaky.py", body)
    write(repo, "app/raising.py", body)
    violations = json.loads(code_map.generate(repo)[code_map.MAP_JSON])["seams"]["violations"]
    files = {v["file"] for v in violations if v["rule"] == "agent-tickets-only-raising"}
    assert "app/sneaky.py" in files and "app/raising.py" not in files


def test_allowlist_marks_but_never_hides(tmp_path):
    repo = fixture_repo(tmp_path)
    write(
        repo,
        "tools/code_map_allowlist.json",
        json.dumps([{"rule": "routers-only-from-main", "file": "app/other.py", "in": "<module>", "reason": "because"}]),
    )
    violations = json.loads(code_map.generate(repo)[code_map.MAP_JSON])["seams"]["violations"]
    other = [v for v in violations if v["file"] == "app/other.py"]
    assert other and other[0]["allowed"] == "because"
    write(repo, "tools/code_map_allowlist.json", json.dumps([{"rule": "x", "file": "y", "reason": " "}]))
    with pytest.raises(SystemExit):
        code_map.generate(repo)


def test_check_fails_on_a_stale_map_and_passes_when_current(tmp_path, capsys):
    repo = fixture_repo(tmp_path)
    assert code_map.main(["--root", str(repo), "--check"]) == 1  # nothing committed yet
    assert code_map.main(["--root", str(repo)]) == 0
    assert code_map.main(["--root", str(repo), "--check"]) == 0
    router = repo / "app/routers/good.py"
    write(repo, "app/routers/good.py", router.read_text() + '\n@router.get("/added")\ndef added():\n    return 1\n')
    assert code_map.main(["--root", str(repo), "--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    assert code_map.main(["--root", str(repo), "--strict"]) == 1  # open violations
    # A hand edit of the generated file is caught too.
    code_map.main(["--root", str(repo)])
    md = repo / code_map.MAP_MD
    md.write_text(md.read_text() + "hand edit\n", newline="\n")
    assert code_map.main(["--root", str(repo), "--check"]) == 1


def test_the_committed_map_is_current():
    """The same check CI runs: fails when someone changed code without regenerating the map."""
    assert code_map.main(["--check"]) == 0
