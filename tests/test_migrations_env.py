import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parent.parent


def percent_encode_first_character(password: str) -> str:
    """The same password, written the way the Bicep writes special characters."""
    return f"%{ord(password[0]):02X}{password[1:]}"


def test_alembic_accepts_a_url_encoded_password():
    """The Bicep builds the database URL with uriComponent(), so a password with
    @ / : and so on arrives containing %. Alembic's config reader treats % as an
    interpolation and used to refuse to start, which would fail the migrate job."""
    parsed = make_url(TEST_DATABASE_URL)
    encoded_url = (
        f"{parsed.drivername}://{parsed.username}:{percent_encode_first_character(parsed.password)}"
        f"@{parsed.host}:{parsed.port}/{parsed.database}"
    )
    assert "%" in encoded_url

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": encoded_url, "ENV": "dev"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "invalid interpolation" not in result.stderr
