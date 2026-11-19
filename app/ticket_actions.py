"""The action chips of the agent ticket page (#254): which actions the header offers for a ticket in its present state, and
which card each one opens.

This decides only what to DRAW. Whether a person may really do an action is decided where it always was (app/visibility.py,
app/permissions.py and each service's own check, which the route runs again when the form is posted); the page passes the
answers in as plain booleans so the rules for the row can be read, and tested, in one place:

- Email user and Add note come first.
- An untriaged ticket offers Triage in place of Re-assign and Escalate; a triaged one offers Re-assign and Escalate.
- A ticket at the MSP offers Take back in place of Send to MSP.
- Link and Split only when the agent may do them.
- Resolve is the one primary button; once the ticket is resolved it is Reopen (or Reclose).
- Backdate and Close sit under the small more button.

A chip is a link to `?action=<key>` (so it works without scripts); with htmx it opens the same card in place. The key is
also the name of the card, so a stale or invented `?action=` opens nothing."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Chip:
    key: str
    label: str
    primary: bool = False
    post: bool = False  # a plain button that acts at once (Assign to me), not a link to a card


@dataclass
class Chips:
    row: list[Chip] = field(default_factory=list)  # left to right, Email user and Add note first
    primary: Optional[Chip] = None  # Resolve, Reopen or Reclose (Triage is the primary chip of an untriaged ticket)
    more: list[Chip] = field(default_factory=list)  # under the small more button

    def keys(self) -> list[str]:
        """Every card this ticket may open, in the order drawn."""
        keys = [c.key for c in self.row if not c.post]
        if self.primary is not None:
            keys.append(self.primary.key)
        return keys + [c.key for c in self.more]


# The card's heading and the name the presence strip shows while it is open ("Bob is re-assigning"). The composers (email,
# note) have no name: they only count once something is typed (app/static/ticket-presence.js).
CARDS: dict[str, tuple[str, str]] = {
    "email": ("Email the requester", ""),
    "note": ("Add a note", ""),
    "triage": ("Triage", "triaging it"),
    "assign": ("Assign without triaging", "assigning"),
    "reassign": ("Re-assign", "re-assigning"),
    "escalate": ("Escalate", "escalating"),
    "msp": ("Send to the MSP", "sending to the MSP"),
    "takeback": ("Take back from the MSP", "taking it back from the MSP"),
    "link": ("Link under a Major Incident or Problem", "linking it"),
    "split": ("Split ticket", "splitting it"),
    "backdate": ("Backdate (log after the fact)", "backdating it"),
    "close": ("Close", "closing it"),
    "resolve": ("Resolve", "resolving"),
    "reopen": ("Reopen", "reopening"),
    "reclose": ("Reclose", "reclosing"),
}


def action_chips(
    *,
    can_work: bool,
    can_note: bool,
    triaged: bool,  # triaged_at is set
    triaged_actions: bool,  # triaged and not waiting for approval: Resolve, Reopen, Reclose and Close are about this
    can_triage: bool,
    is_super_admin: bool,
    can_self_assign: bool,
    assigned: bool,
    can_reassign: bool,
    with_msp: bool,
    can_take_back: bool,
    can_link: bool,
    can_split: bool,
    can_resolve: bool,
    can_reopen: bool,
    can_reclose: bool,
    can_close: bool,
    can_backdate: bool,
) -> Chips:
    chips = Chips()
    if can_work:
        chips.row.append(Chip("email", "Email user"))
    if can_note:
        chips.row.append(Chip("note", "Add note"))
    if not can_work:
        return chips  # read-only: a note while the ticket is untriaged, and nothing else

    if not assigned and can_self_assign:
        chips.row.append(Chip("assign_me", "Assign to me", post=True))
    if can_triage:
        # An untriaged ticket has no Resolve, so the one thing to do next is the primary button.
        chips.row.append(Chip("triage", "Triage", primary=True))
        if is_super_admin:
            chips.row.append(Chip("assign", "Assign"))
    else:
        if can_reassign:
            chips.row.append(Chip("reassign", "Re-assign" if assigned else "Assign"))
        if triaged:
            chips.row.append(Chip("escalate", "Escalate"))

    if with_msp:
        if can_take_back:
            chips.row.append(Chip("takeback", "Take back"))
    else:
        chips.row.append(Chip("msp", "Send to MSP"))
    if can_link:
        chips.row.append(Chip("link", "Link"))
    if can_split:
        chips.row.append(Chip("split", "Split"))

    if triaged_actions and can_resolve:
        chips.primary = Chip("resolve", "Resolve", primary=True)
    elif triaged_actions and can_reopen:
        chips.primary = Chip("reopen", "Reopen", primary=True)
    elif triaged_actions and can_reclose:
        chips.primary = Chip("reclose", "Reclose", primary=True)
    if triaged_actions and can_reopen and can_reclose:
        chips.row.append(Chip("reclose", "Reclose"))

    if can_backdate:
        chips.more.append(Chip("backdate", "Backdate"))
    if triaged_actions and can_close:
        chips.more.append(Chip("close", "Close"))
    return chips
