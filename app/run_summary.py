"""What one scheduled job run did (#341): named counts and the tickets it changed.

Lives in its own module so the job modules can return it without importing `app/scheduled_jobs.py` (which imports them).
`app.scheduled_jobs.RunSummary` is the same class. A summary holds no free text and no person data: the sentence an admin
reads is built from `app/job_runs.py`'s fixed wording for each job and count key.
"""

from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class RunSummary:
    counts: dict[str, int] = field(default_factory=dict)
    ticket_ids: list[int] = field(default_factory=list)

    @classmethod
    def of(cls, ticket_ids: Optional[Iterable[int]] = None, **counts: int) -> "RunSummary":
        """RunSummary.of(closed=3) or RunSummary.of(ids, closed=len(ids)). Zero counts are kept: the key says what was looked for."""
        return cls(dict(counts), list(ticket_ids or ()))

    def merge(self, other: "RunSummary") -> "RunSummary":
        for key, number in other.counts.items():
            self.counts[key] = self.counts.get(key, 0) + number
        self.ticket_ids.extend(i for i in other.ticket_ids if i not in self.ticket_ids)
        return self

    @property
    def did_work(self) -> bool:
        return bool(self.ticket_ids) or any(self.counts.values())


def adapt(result) -> Optional[RunSummary]:
    """What a job's run returned as a RunSummary: a RunSummary as it is, None as None, an int as {"done": n} (a small adapter for
    a job that only counts). Anything else is not a summary."""
    if result is None or isinstance(result, RunSummary):
        return result
    if isinstance(result, int) and not isinstance(result, bool):
        return RunSummary({"done": result})
    return None
