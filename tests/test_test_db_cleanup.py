"""The test databases are dropped when a session ends (tests/conftest.py), and tools/clean_test_dbs.py
removes leftovers safely. The scratch databases here carry a unique prefix so nothing else is touched."""

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL, drop_created_databases

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("clean_test_dbs", ROOT / "tools" / "clean_test_dbs.py")
clean_test_dbs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clean_test_dbs)

PARSED = make_url(TEST_DATABASE_URL)


def connect(dbname="postgres"):
    connection = psycopg2.connect(
        host=PARSED.host, port=PARSED.port, user=PARSED.username, password=PARSED.password, dbname=dbname
    )
    connection.autocommit = True
    return connection


def database_names(prefix):
    connection = connect()
    with connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (prefix + "%",))
        names = sorted(row[0] for row in cursor.fetchall())
    connection.close()
    return names


@pytest.fixture
def prefix():
    name = f"ticketing_clean{uuid.uuid4().hex[:8]}"
    yield name
    connection = connect()
    with connection.cursor() as cursor:
        for leftover in database_names(name):
            cursor.execute(f'DROP DATABASE IF EXISTS "{leftover}"')
    connection.close()


def create(name):
    connection = connect()
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    connection.close()


def run_nested_pytest(prefix, extra_env=None, workers=None):
    """A real pytest session (one small test file) on its own database name."""
    url = PARSED.set(database=f"{prefix}_test").render_as_string(hide_password=False)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_XDIST_WORKER", "KEEP_TEST_DB")}
    env["TEST_DATABASE_URL"] = url
    env.update(extra_env or {})
    args = [sys.executable, "-m", "pytest", "tests/test_health.py", "-q", "-p", "no:cacheprovider"]
    if workers:
        args += ["-n", str(workers)]
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr


def test_session_drops_the_databases_it_created(prefix):
    run_nested_pytest(prefix)
    assert database_names(prefix) == []


def test_xdist_workers_and_controller_drop_theirs(prefix):
    run_nested_pytest(prefix, workers=2)
    assert database_names(prefix) == []


def test_keep_test_db_keeps_them(prefix):
    run_nested_pytest(prefix, {"KEEP_TEST_DB": "1"})
    assert database_names(prefix) == [f"{prefix}_test"]


def test_a_database_that_existed_before_the_run_is_not_dropped(prefix):
    create(f"{prefix}_test")
    run_nested_pytest(prefix)
    assert database_names(prefix) == [f"{prefix}_test"]


def test_session_drop_refuses_a_name_not_ending_in_test(prefix):
    create(prefix)  # a name that does not end in _test
    assert drop_created_databases(TEST_DATABASE_URL, [prefix]) == []
    assert database_names(prefix) == [prefix]


def test_script_does_nothing_without_yes(prefix):
    create(f"{prefix}_a_test")
    connection = connect()
    with connection.cursor() as cursor:
        assert clean_test_dbs.clean(cursor, yes=False, out=lambda line: None, prefix=prefix) == []
    connection.close()
    assert database_names(prefix) == [f"{prefix}_a_test"]


def test_script_never_lists_a_name_not_ending_in_test(prefix):
    create(prefix)
    create(f"{prefix}_b_test")
    connection = connect()
    with connection.cursor() as cursor:
        names = [row[0] for row in clean_test_dbs.list_test_databases(cursor, prefix)]
        dropped = clean_test_dbs.clean(cursor, yes=True, out=lambda line: None, prefix=prefix)
    connection.close()
    assert names == [f"{prefix}_b_test"]
    assert dropped == [f"{prefix}_b_test"]
    assert database_names(prefix) == [prefix]


def test_the_dev_database_cannot_be_matched():
    assert not clean_test_dbs.is_droppable("ticketing")
    assert not clean_test_dbs.is_droppable("_test")
    assert clean_test_dbs.is_droppable("ticketing_x_gw0_test")


def test_script_skips_a_database_with_an_open_connection(prefix):
    create(f"{prefix}_open_test")
    create(f"{prefix}_idle_test")
    held = connect(f"{prefix}_open_test")
    try:
        connection = connect()
        lines = []
        with connection.cursor() as cursor:
            dropped = clean_test_dbs.clean(cursor, yes=True, out=lines.append, prefix=prefix)
        connection.close()
        assert dropped == [f"{prefix}_idle_test"]
        assert any(line.startswith("SKIP") and f"{prefix}_open_test" in line for line in lines)
        assert database_names(prefix) == [f"{prefix}_open_test"]
    finally:
        held.close()
