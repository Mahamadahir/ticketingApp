"""Exits non-zero when the environment is unsafe to run in.

The container entrypoint runs this before migrations and uvicorn. The same
check in the app's lifespan isn't enough on its own: with multiple uvicorn
workers a failed worker startup leaves the parent process running.
"""
import sys

from app.config import settings, unsafe_setting_errors


def main() -> int:
    errors = unsafe_setting_errors(settings)
    for error in errors:
        print(f"Refusing to start: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
