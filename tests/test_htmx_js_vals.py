"""htmx wraps a `js:` hx-vals that does not start with `{` in braces, so `js:f()` becomes `({f()})`, a syntax error
that stops htmx setting up the rest of the page. Every `js:` hx-vals in a template must be an object literal."""

import pytest
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
JS_VALS = re.compile(r"""hx-vals\s*=\s*(["'])\s*(?:js|javascript):(.*?)\1""", re.S)


@pytest.mark.guard
def test_every_js_hx_vals_is_an_object_literal():
    bad = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for match in JS_VALS.finditer(path.read_text(encoding="utf-8")):
            if not match.group(2).lstrip().startswith("{"):
                bad.append(f"{path.name}: {match.group(0)}")
    assert not bad, "hx-vals js: must start with '{' (use js:{...f()}):\n" + "\n".join(bad)
