"""Lists and drops leftover `*_test` databases (crashed runs, old branches).

    python tools/clean_test_dbs.py            # list names, sizes and open connections
    python tools/clean_test_dbs.py --yes      # drop the ones with no open connections
    python tools/clean_test_dbs.py --prefix ticketing_mybranch --yes   # only names starting so

Only databases whose name ends in `_test` are ever considered, so the dev database `ticketing`
cannot be listed or dropped. A database with an open connection is skipped. Connection details
come from TEST_DATABASE_URL (default: the local compose db); only host, port and credentials are used.
"""

import argparse
import os
import sys

import psycopg2
from sqlalchemy.engine import make_url

DEFAULT_URL = "postgresql+psycopg2://ticketing:ticketing@127.0.0.1:5432/ticketing_test"


def is_droppable(name: str) -> bool:
    """The one safety rule: a name must end in _test (and have something before it)."""
    return name.endswith("_test") and len(name) > len("_test")


def list_test_databases(cursor, prefix: str = "") -> list[tuple[str, int, int]]:
    """(name, size in bytes, open connections) for every *_test database."""
    cursor.execute(
        "SELECT d.datname, pg_database_size(d.datname), "
        "(SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) "
        "FROM pg_database d WHERE d.datistemplate = false AND d.datname LIKE %s ORDER BY d.datname",
        (r"%\_test",),
    )
    return [row for row in cursor.fetchall() if is_droppable(row[0]) and row[0].startswith(prefix)]


def clean(cursor, yes: bool, out=print, prefix: str = "") -> list[str]:
    """Reports every *_test database; with `yes`, drops those with no open connection. Returns the dropped names."""
    dropped = []
    for name, size, connections in list_test_databases(cursor, prefix):
        megabytes = size / 1024 / 1024
        if connections:
            out(f"SKIP  {name} ({megabytes:.1f} MB): {connections} open connection(s)")
        elif not yes:
            out(f"found {name} ({megabytes:.1f} MB)")
        else:
            cursor.execute(f'DROP DATABASE "{name}"')  # no FORCE: a connection that appears since the check fails the drop
            dropped.append(name)
            out(f"DROP  {name} ({megabytes:.1f} MB)")
    if not yes:
        out("Nothing dropped. Run again with --yes to drop the databases listed as found.")
    return dropped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="drop the listed databases (without it, nothing is dropped)")
    parser.add_argument("--prefix", default="", help="only consider databases whose name starts with this")
    args = parser.parse_args(argv)
    parsed = make_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_URL))
    connection = psycopg2.connect(
        host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname="postgres"
    )
    connection.autocommit = True
    with connection.cursor() as cursor:
        clean(cursor, args.yes, prefix=args.prefix)
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
