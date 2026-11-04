from fastapi import HTTPException, Request

MAX_MESSAGE_LENGTH = 10_000
ANSWER_FIELD_PREFIX = "q_"


async def form_answers(request: Request) -> dict[int, str]:
    """The answers to a request type's form questions on a posted form, by question id: the fields named q_<id>.
    A dependency, because the questions differ with the kind of request, so the fields are not known up front.
    Text only; whether each answer is valid is decided in app/request_questions.py."""
    answers: dict[int, str] = {}
    for key, value in (await request.form()).multi_items():
        number = key[len(ANSWER_FIELD_PREFIX):]
        if key.startswith(ANSWER_FIELD_PREFIX) and number.isascii() and number.isdecimal() and len(number) < 10 and isinstance(value, str):
            answers[int(number)] = value
    return answers


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
