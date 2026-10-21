"""Generates the code map: docs/code-map.md and docs/code-map.json (stdlib only).

    python tools/code_map.py            regenerate both files (commit them)
    python tools/code_map.py --check    exit 1 if the committed files are stale (CI)
    python tools/code_map.py --strict   also exit 1 on any seam violation not in the allowlist
    python tools/code_map.py --root DIR map another checkout (the tests use this)

The map is built from `ast` only, nothing is imported or run, and the output is deterministic
(sorted, no timestamps, LF line endings). Never edit the generated files by hand.

It records, per module under app/: the first line of the docstring, public functions and classes,
which modules import it, the routes it declares, the tables it defines, the settings, the worker
jobs, the Alembic chain and the test files that import each module. It also checks the
architecture seams (SEAMS below) and lists the violations it finds. Violations are never hidden:
tools/code_map_allowlist.json can excuse an entry, but each entry needs a reason and is still
printed, marked allowed.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
MAP_MD = "docs/code-map.md"
MAP_JSON = "docs/code-map.json"
ALLOWLIST = "tools/code_map_allowlist.json"
SECRET_WORDS = ("secret", "password", "key", "token", "credential")

# The rules from CLAUDE.md that the script can see in the syntax tree. Each is checked in
# check_seams(); the text here is what the map prints.
SEAMS = {
    "access-in-visibility": (
        "Who can see or work a ticket is decided only in app/visibility.py. Routes do not query "
        "Ticket, TeamMembership or TicketEscalator themselves unless the same function applies an "
        "app.visibility helper."
    ),
    "messages-via-messages-module": (
        "What the requester may see, and whether a ticket can be emailed, is decided only in "
        "app/messages.py. Nothing else reads Ticket.messages or runs select() on Message."
    ),
    "agent-tickets-only-raising": (
        "Only app/raising.py creates source=agent tickets (backdating relies on it)."
    ),
    "routers-only-from-main": (
        "Only app/main.py includes routers. Other modules do not import app.routers.*."
    ),
    "models-is-a-leaf": "app/models.py imports no other app module except app.config.",
    "tickets-via-ticket-service": (
        "Tickets are created and change status through app/ticket_service.py (numbers come from "
        "the models hook). Nothing else sets Ticket(...) directly outside the allowed modules."
    ),
}
# Modules allowed to build Ticket(...) rows (rule tickets-via-ticket-service).
TICKET_BUILDERS = {"app/ticket_service.py", "app/models.py"}


# ----------------------------------------------------------------------------- parsing helpers

def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_name(relpath: str) -> str:
    parts = relpath[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def first_line(doc: str | None) -> str:
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def py_files(root: Path, sub: str) -> list[Path]:
    base = root / sub
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def imports_of(tree: ast.Module, current: str, known: set[str]) -> set[str]:
    """Internal modules (names in `known`) this tree imports, including imports inside functions."""
    found: set[str] = set()
    package = current.rsplit(".", 1)[0] if "." in current else current

    def add(name: str) -> None:
        # Credit the longest known module that is a prefix of the dotted name.
        parts = name.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in known:
                found.add(candidate)
                return
            parts.pop()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                base_parts = current.split(".")[: -node.level] if node.level else []
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if not base:
                continue
            for alias in node.names:
                full = f"{base}.{alias.name}"
                if full in known:  # from app.routers import admin
                    found.add(full)
                else:
                    add(base)
    found.discard(current)
    del package
    return found


def public_names(tree: ast.Module) -> tuple[list[str], list[str]]:
    funcs, classes = [], []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            funcs.append(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            classes.append(node.name)
    return sorted(funcs), sorted(classes)


def string_value(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def routers_in(tree: ast.Module) -> dict[str, str]:
    """Module-level `name = APIRouter(prefix=...)` -> prefix. Also FastAPI(...) apps (prefix '')."""
    result: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            fname = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if fname in ("APIRouter", "FastAPI"):
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        prefix = string_value(kw.value) or ""
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        result[target.id] = prefix
    return result


def routes_in(tree: ast.Module) -> list[dict]:
    routers = routers_in(tree)
    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in HTTP_METHODS
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id in routers
                and dec.args
            ):
                path = string_value(dec.args[0])
                if path is not None:
                    routes.append(
                        {
                            "method": dec.func.attr.upper(),
                            "path": routers[dec.func.value.id] + path,
                            "handler": node.name,
                        }
                    )
    return sorted(routes, key=lambda r: (r["path"], r["method"], r["handler"]))


def tables_in(tree: ast.Module) -> list[dict]:
    tables = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(kw.arg == "table" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords):
            continue
        name = node.name.lower()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets):
                name = string_value(stmt.value) or name
        tables.append({"class": node.name, "table": name})
    return sorted(tables, key=lambda t: t["class"])


def settings_in(tree: ast.Module) -> list[dict]:
    result = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    name = stmt.target.id
                    default = ast.unparse(stmt.value) if stmt.value is not None else ""
                    if any(word in name for word in SECRET_WORDS):
                        default = "(secret)"
                    result.append({"name": name, "env": name.upper(), "type": ast.unparse(stmt.annotation), "default": default})
    return sorted(result, key=lambda s: s["name"])


def jobs_in(tree: ast.Module) -> list[dict]:
    jobs = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        elif isinstance(node, ast.Assign):
            targets, value = [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
        else:
            continue
        if "JOBS" in targets and isinstance(value, ast.List):
            for element in value.elts:
                if isinstance(element, ast.Call) and len(element.args) >= 3 and string_value(element.args[0]):
                    jobs.append(
                        {
                            "name": string_value(element.args[0]),
                            "every": ast.unparse(element.args[1]),
                            "function": ast.unparse(element.args[2]),
                        }
                    )
    return sorted(jobs, key=lambda j: j["name"])


def migrations(root: Path) -> dict:
    revisions: dict[str, list[str]] = {}
    files: dict[str, str] = {}
    for path in py_files(root, "migrations/versions"):
        tree = parse(path)
        rev, down = None, []
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
            else:
                continue
            if name == "revision" and value is not None:
                rev = string_value(value)
            elif name == "down_revision" and value is not None:
                if string_value(value):
                    down = [string_value(value)]
                elif isinstance(value, (ast.Tuple, ast.List)):
                    down = [string_value(e) for e in value.elts if string_value(e)]
        if rev:
            revisions[rev] = down
            files[rev] = rel(path, root)
    parents = {d for downs in revisions.values() for d in downs}
    heads = sorted(r for r in revisions if r not in parents)
    chain: list[str] = []
    cursor = heads[0] if heads else None
    while cursor and cursor not in chain:
        chain.append(cursor)
        downs = revisions.get(cursor, [])
        cursor = downs[0] if downs else None
    return {
        "heads": [{"revision": h, "file": files[h]} for h in heads],
        "count": len(revisions),
        "chain_tail": [{"revision": r, "file": files[r]} for r in chain[:5]],
    }


# ----------------------------------------------------------------------------- seam checks

def walk_with_scope(tree: ast.Module):
    """Yields (node, name of the enclosing function or '<module>'). Line numbers are left out of the
    map on purpose: they change with every edit and would make the committed map stale constantly."""
    stack = [(tree, "<module>")]
    while stack:
        node, scope = stack.pop()
        yield node, scope
        inner = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def check_seams(trees: dict[str, ast.Module], imports: dict[str, set[str]]) -> list[dict]:
    """Violations of SEAMS, as {rule, file, line, detail}. Pure function of the parsed trees."""
    found: list[dict] = []

    def add(rule: str, file: str, scope: str, detail: str) -> None:
        found.append({"rule": rule, "file": file, "in": scope, "detail": detail})

    for file, tree in trees.items():
        is_router = file.startswith("app/routers/")
        # Names imported from app.visibility, and the functions that use any of them: a route that
        # selects tickets AND applies a visibility helper is doing it the right way.
        from_visibility = {
            alias.asname or alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "app.visibility"
            for alias in n.names
        }
        uses_visibility = {scope for n, scope in walk_with_scope(tree) if isinstance(n, ast.Name) and n.id in from_visibility}
        for node, scope in walk_with_scope(tree):
            # access-in-visibility: a route querying the access tables itself
            if is_router and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "select":
                hit = _names_in(node) & {"Ticket", "TeamMembership", "TicketEscalator"}
                if hit and scope not in uses_visibility:
                    add("access-in-visibility", file, scope, "select(" + ", ".join(sorted(hit)) + ") in a route")
            # messages-via-messages-module
            if file not in ("app/messages.py", "app/models.py"):
                if isinstance(node, ast.Attribute) and node.attr == "messages":
                    add("messages-via-messages-module", file, scope, f"reads .messages ({ast.unparse(node)})")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "select":
                    if "Message" in _names_in(node):
                        add("messages-via-messages-module", file, scope, "select() on Message")
            # agent-tickets-only-raising
            if file != "app/raising.py":
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "source":
                            v = kw.value
                            if (isinstance(v, ast.Attribute) and v.attr == "AGENT" and "TicketSource" in _names_in(v)) or string_value(v) == "agent":
                                add("agent-tickets-only-raising", file, scope, "creates a source=agent ticket")
            # tickets-via-ticket-service
            if (
                file not in TICKET_BUILDERS
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Ticket"
            ):
                add("tickets-via-ticket-service", file, scope, "builds Ticket(...) directly")

    for file, targets in imports.items():
        if file == "app/main.py":
            continue
        for target in sorted(targets):
            if target.startswith("app.routers") and target != "app.routers" and not file.startswith("app/routers/"):
                add("routers-only-from-main", file, "<module>", f"imports {target}")
    if "app/models.py" in imports:
        for target in sorted(imports["app/models.py"]):
            if target not in ("app.config",):
                add("models-is-a-leaf", "app/models.py", "<module>", f"imports {target}")

    return sorted(found, key=lambda v: (v["rule"], v["file"], v["in"], v["detail"]))


def load_allowlist(root: Path) -> list[dict]:
    path = root / ALLOWLIST
    if not path.is_file():
        return []
    entries = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        if not entry.get("reason", "").strip() or "rule" not in entry or "file" not in entry:
            raise SystemExit(f"{ALLOWLIST}: every entry needs rule, file and a non-empty reason: {entry}")
    return entries


def apply_allowlist(violations: list[dict], allowlist: list[dict]) -> list[dict]:
    for v in violations:
        for entry in allowlist:
            if entry["rule"] == v["rule"] and entry["file"] == v["file"] and entry.get("contains", "") in v["detail"]:
                v["allowed"] = entry["reason"]
                break
    return violations


# ----------------------------------------------------------------------------- the map

def build_map(root: Path) -> dict:
    app_files = py_files(root, "app")
    trees = {rel(p, root): parse(p) for p in app_files}
    names = {module_name(f): f for f in trees}
    known = set(names)

    imports = {f: imports_of(t, module_name(f), known) for f, t in trees.items()}
    imported_by: dict[str, set[str]] = {m: set() for m in known}
    for f, targets in imports.items():
        for target in targets:
            imported_by[target].add(module_name(f))

    test_files = {rel(p, root): parse(p) for p in py_files(root, "tests")}
    tested_by: dict[str, set[str]] = {m: set() for m in known}
    for tf, tree in test_files.items():
        for target in imports_of(tree, "tests." + Path(tf).stem, known):
            tested_by[target].add(tf)

    modules = []
    for f in sorted(trees):
        tree, name = trees[f], module_name(f)
        funcs, classes = public_names(tree)
        entry = {
            "module": name,
            "file": f,
            "summary": first_line(ast.get_docstring(tree)),
            "functions": funcs,
            "classes": classes,
            "imports": sorted(imports[f]),
            "imported_by": sorted(imported_by[name]),
            "routes": routes_in(tree),
            "tables": tables_in(tree),
            "tests": sorted(tested_by[name]),
        }
        modules.append(entry)

    jobs, settings = [], []
    if "app/jobs/__init__.py" in trees:
        jobs = jobs_in(trees["app/jobs/__init__.py"])
    if "app/config.py" in trees:
        settings = settings_in(trees["app/config.py"])

    violations = apply_allowlist(check_seams(trees, imports), load_allowlist(root))
    return {
        "modules": modules,
        "jobs": jobs,
        "settings": settings,
        "migrations": migrations(root),
        "tests": sorted(test_files),
        "seams": {"rules": SEAMS, "violations": violations},
    }


def render_md(data: dict) -> str:
    out: list[str] = []
    w = out.append
    w("# Code map")
    w("")
    w("Generated by `python tools/code_map.py`. Do not edit by hand: regenerate and commit it with")
    w("any change that adds, removes or renames modules, routes, models, jobs, settings, migrations or tests")
    w("(CI job `code-map-current` fails otherwise). The same data is in `docs/code-map.json`.")
    w("")
    mg = data["migrations"]
    w("## Contents")
    w("")
    w("- Architecture seams and violations")
    w("- Routes, tables, worker jobs, settings, migrations")
    w("- Modules (what each owns, public names, imports, imported by, tests)")
    w("")
    w("## Architecture seams")
    w("")
    for rule, text in data["seams"]["rules"].items():
        w(f"- `{rule}`: {text}")
    w("")
    w("### Current violations")
    w("")
    violations = data["seams"]["violations"]
    if not violations:
        w("None.")
    for v in violations:
        where = f"{v['file']} in `{v['in']}`"
        suffix = f" (allowed: {v['allowed']})" if "allowed" in v else ""
        w(f"- `{v['rule']}` {where}: {v['detail']}{suffix}")
    w("")
    w("## Routes")
    w("")
    routes = [(r, m["module"]) for m in data["modules"] for r in m["routes"]]
    w("| Method | Path | Handler | Module |")
    w("|---|---|---|---|")
    for r, mod in sorted(routes, key=lambda x: (x[0]["path"], x[0]["method"], x[1])):
        w(f"| {r['method']} | `{r['path']}` | `{r['handler']}` | `{mod}` |")
    w("")
    w("## Tables")
    w("")
    w("| Table | Class | Module |")
    w("|---|---|---|")
    for m in data["modules"]:
        for t in m["tables"]:
            w(f"| `{t['table']}` | `{t['class']}` | `{m['module']}` |")
    w("")
    w("## Worker jobs (`app/jobs/__init__.py`)")
    w("")
    w("| Job | Every | Function |")
    w("|---|---|---|")
    for j in data["jobs"]:
        w(f"| `{j['name']}` | `{j['every']}` | `{j['function']}` |")
    w("")
    w("## Settings (`app/config.py`)")
    w("")
    w("| Env var | Type | Default |")
    w("|---|---|---|")
    for s in data["settings"]:
        w(f"| `{s['env']}` | `{s['type']}` | `{s['default']}` |")
    w("")
    w("## Migrations")
    w("")
    w(f"{mg['count']} revisions. Head: " + ", ".join(f"`{h['revision']}`" for h in mg["heads"]) + ".")
    if len(mg["heads"]) > 1:
        w("MORE THAN ONE HEAD: merge them.")
    w("")
    w("Chain tail (newest first):")
    w("")
    for c in mg["chain_tail"]:
        w(f"- `{c['revision']}` {c['file']}")
    w("")
    w("## Modules")
    w("")
    w("Tests listed are the test files that import the module. A test that reaches it only through")
    w("the HTTP client (routes) does not appear here: grep `tests/` for the path.")
    w("")
    for m in data["modules"]:
        w(f"### `{m['module']}`")
        w("")
        w(f"{m['summary'] or '(no docstring)'}  ")
        w(f"File: `{m['file']}`")
        if m["classes"]:
            w("")
            w("- Classes: " + ", ".join(f"`{c}`" for c in m["classes"]))
        if m["functions"]:
            w("- Functions: " + ", ".join(f"`{f}`" for f in m["functions"]))
        if m["routes"]:
            w("- Routes: " + ", ".join(f"{r['method']} `{r['path']}`" for r in m["routes"]))
        if m["tables"]:
            w("- Tables: " + ", ".join(f"`{t['table']}`" for t in m["tables"]))
        w("- Imports: " + (", ".join(f"`{i}`" for i in m["imports"]) or "none"))
        w("- Imported by: " + (", ".join(f"`{i}`" for i in m["imported_by"]) or "none"))
        w("- Tests: " + (", ".join(f"`{Path(t).name}`" for t in m["tests"]) or "none"))
        w("")
    return "\n".join(out).rstrip("\n") + "\n"


def render_json(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generate(root: Path) -> dict[str, str]:
    data = build_map(root)
    return {MAP_MD: render_md(data), MAP_JSON: render_json(data)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed map is stale")
    parser.add_argument("--strict", action="store_true", help="fail on seam violations that are not allowlisted")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    outputs = generate(root)
    status = 0

    if args.check:
        for relpath, text in outputs.items():
            path = root / relpath
            current = path.read_bytes().decode("utf-8") if path.is_file() else None
            if current != text:
                print(f"{relpath} is out of date. Run: python tools/code_map.py   and commit the result.", file=sys.stderr)
                status = 1
        if status == 0:
            print("code map is current")
    else:
        for relpath, text in outputs.items():
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        print(f"wrote {MAP_MD} and {MAP_JSON}")

    if args.strict:
        open_violations = [v for v in json.loads(outputs[MAP_JSON])["seams"]["violations"] if "allowed" not in v]
        for v in open_violations:
            print(f"seam violation: {v['rule']} {v['file']} in {v['in']}: {v['detail']}", file=sys.stderr)
        if open_violations:
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
