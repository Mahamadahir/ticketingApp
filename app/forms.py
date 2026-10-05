def optional_int(value: str | None) -> int | None:
    """Converts an HTML select's empty-string 'none' option to None.

    FastAPI's Form(None) on an Optional[int] param rejects "" outright
    (422), which is exactly what a <select><option value="">...</option>
    submits. Route handlers take the raw string and convert with this.
    """
    return int(value) if value else None
