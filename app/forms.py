from fastapi import HTTPException

MAX_MESSAGE_LENGTH = 10_000


def optional_int(value: str | None) -> int | None:
    """Converts an HTML select's empty-string 'none' option to None.

    FastAPI's Form(None) on an Optional[int] param rejects "" outright
    (422), which is exactly what a <select><option value="">...</option>
    submits. Route handlers take the raw string and convert with this.
    A value that is not a number is a 400, not a server error.
    """
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Expected a number")
