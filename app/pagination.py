from dataclasses import dataclass, field
from math import ceil
from typing import Any, Mapping
from urllib.parse import urlencode

from sqlmodel import Session, func, select

PAGE_SIZE = 50
# Keeps OFFSET far inside bigint; nobody pages this deep legitimately.
MAX_PAGE = 100_000


@dataclass
class Page:
    items: list[Any]
    number: int
    total_items: int
    # The list's other query parameters (filters), carried into page links.
    query_params: dict[str, str] = field(default_factory=dict)
    size: int = PAGE_SIZE

    @property
    def total_pages(self) -> int:
        return max(1, ceil(self.total_items / self.size))

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages

    def link(self, page_number: int) -> str:
        return "?" + urlencode({**self.query_params, "page": page_number})


def paginate(session: Session, query, page_number: int, query_params: Mapping[str, str]) -> Page:
    total_items = session.exec(select(func.count()).select_from(query.subquery())).one()
    page_number = min(max(1, page_number), MAX_PAGE)
    items = session.exec(query.offset((page_number - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    filters = {key: value for key, value in query_params.items() if key != "page" and value}
    return Page(items=list(items), number=page_number, total_items=total_items, query_params=filters)
