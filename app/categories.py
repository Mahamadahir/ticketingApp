"""Ticket categories: a main category (owned by a team) with optional
sub-categories. The category chosen at triage decides which team gets the ticket.

"Other" is a seeded main category for tickets nothing fits. The ticket is worked
and resolved normally; afterwards a super admin gives it a proper category (see
the review queue in the admin routes)."""

from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, select

from app.models import Category, Team
from app.settings_store import DEFAULT_TRIAGE_TEAM, get_int_setting

MAX_NAME_LENGTH = 80
OTHER_NAME = "Other"


# One fixed key for the whole category table. A uniqueness constraint cannot stop two
# main categories sharing a name, because their parent is NULL and Postgres treats
# NULLs as distinct, so creation takes this lock for the rest of its transaction.
_CATEGORY_LOCK = 7101


class CategoryError(ValueError):
    """A problem with a category the user can be told about."""


def _lock_categories(session: Session) -> None:
    session.exec(text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=_CATEGORY_LOCK))


def is_selectable(session: Session, category: Category) -> bool:
    """Whether a category can be given to a ticket: it is on, and so is its parent.
    Checked again when a form is submitted, because a stale page or a crafted
    request can name a category that has since been switched off."""
    if not category.is_active:
        return False
    if category.parent_id is None:
        return True
    parent = session.get(Category, category.parent_id)
    return parent is not None and parent.is_active


def get_other(session: Session) -> Optional[Category]:
    return session.exec(select(Category).where(Category.is_other.is_(True))).first()


def ensure_other(session: Session) -> Category:
    """The migration seeds Other; this covers a database built without it."""
    other = get_other(session)
    if other is None:
        _lock_categories(session)
        other = get_other(session)  # someone may have made it while we waited
    if other is None:
        other = Category(name=OTHER_NAME, is_other=True)
        session.add(other)
        session.flush()
    return other


def team_id_for(session: Session, category: Category) -> Optional[int]:
    """The team that works tickets in this category: its own, else its parent's,
    else (for Other only) the default triage team."""
    if category.team_id is not None:
        return category.team_id
    if category.parent_id is not None:
        parent = session.get(Category, category.parent_id)
        if parent is not None and parent.team_id is not None:
            return parent.team_id
    if category.is_other:
        return get_int_setting(session, DEFAULT_TRIAGE_TEAM)
    return None


def escalation_team_id_for(session: Session, category: Category) -> Optional[int]:
    """The team escalating a ticket in this category sends it to, or None for the MSP
    (the default). A sub-category uses its parent's unless it overrides."""
    if category.parent_id is not None and category.escalation_inherits:
        parent = session.get(Category, category.parent_id)
        if parent is not None:
            return parent.escalation_team_id
    return category.escalation_team_id


def path_of(session: Session, category: Category) -> str:
    """'Hardware / Laptop' for a sub-category, 'Hardware' for a main one."""
    if category.parent_id is None:
        return category.name
    parent = session.get(Category, category.parent_id)
    return f"{parent.name} / {category.name}" if parent else category.name


def selectable(session: Session) -> list[tuple[Category, str]]:
    """Active categories to pick from at triage, main ones followed by their
    sub-categories, as (category, label). Sub-categories of an inactive main
    category are left out."""
    rows = session.exec(select(Category).where(Category.is_active.is_(True))).all()
    mains = sorted((c for c in rows if c.parent_id is None), key=lambda c: (c.is_other, c.name.lower()))
    result: list[tuple[Category, str]] = []
    for main in mains:
        result.append((main, main.name))
        for sub in sorted((c for c in rows if c.parent_id == main.id), key=lambda c: c.name.lower()):
            result.append((sub, f"{main.name} / {sub.name}"))
    return result


def parse_escalation(session: Session, category: Category, choice: str) -> tuple[bool, Optional[int]]:
    """Reads an escalation choice for this category: "inherit" (a sub-category follows its
    parent), "msp", or a team id. Returns (inherits, team_id); None for the team means the MSP.
    A main category cannot inherit, and keeps whatever "inherits" it had (unused for a main one)."""
    inherits, team_id = False, None
    if choice == "inherit":
        if category.parent_id is None:
            raise CategoryError("A main category sets its own escalation target")
        inherits = True
    elif choice != "msp":
        try:
            team_id = int(choice) if choice.isascii() and choice.isdigit() else None
        except ValueError:
            team_id = None
        if team_id is None or session.get(Team, team_id) is None:
            raise CategoryError("That team does not exist")
    if category.parent_id is None:
        inherits = category.escalation_inherits
    return inherits, team_id


def create_category(
    session: Session, name: str, parent_id: Optional[int] = None, team_id: Optional[int] = None,
    escalation: Optional[str] = None,
) -> Category:
    """Creates a main category (needs a team) or a sub-category (inherits the
    parent's team). `escalation` says where escalating a ticket in it goes ("inherit" for
    a sub-category, "msp", or a team id); left out it is the default: a sub-category follows
    its parent, a main category goes to the MSP, which cannot be escalated to yet. Does not commit."""
    _lock_categories(session)  # taken first, so the duplicate check below cannot race
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME_LENGTH:
        raise CategoryError(f"A category name must be 1 to {MAX_NAME_LENGTH} characters")
    if name.lower() == OTHER_NAME.lower():
        raise CategoryError("'Other' already exists and is reserved")

    if parent_id is not None:
        parent = session.get(Category, parent_id)
        if parent is None or not parent.is_active:
            raise CategoryError("That parent category does not exist")
        if parent.parent_id is not None:
            raise CategoryError("Categories go one level deep: a sub-category cannot have its own")
        if parent.is_other:
            raise CategoryError("'Other' cannot have sub-categories")
        team_id = None
    else:
        if team_id is None or session.get(Team, team_id) is None:
            raise CategoryError("A main category needs a team")

    siblings = session.exec(select(Category).where(Category.parent_id == parent_id)).all()
    if any(s.name.lower() == name.lower() for s in siblings):
        raise CategoryError(f"There is already a category called '{name}' here")

    category = Category(name=name, parent_id=parent_id, team_id=team_id)
    if escalation is not None:
        category.escalation_inherits, category.escalation_team_id = parse_escalation(session, category, escalation)
    session.add(category)
    session.flush()
    return category
