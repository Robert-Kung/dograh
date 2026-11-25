"""Route inventory checks (mt-s1-foundation 8.3).

tenant-isolation〈跨租戶隔離測試〉: every route of the FastAPI app SHALL be in
exactly one of three classes of ``route_scope.py``:

- (a) ``ORG_SCOPED`` — org-scoped, proven by isolation tests: each entry names
  node ids under ``tests/tenant_isolation/``; each id SHALL be collected by
  pytest and SHALL actually hit the route when run (hit log, ``route_hits``).
- (b) ``EXEMPT`` — exempt, one reason per route.
- (c) ``PROBED`` — org-scoped, proven by the cross-org probe (8.3a): only
  routes of the frozen first-inventory snapshot (``route_probe_snapshot.txt``,
  shrink-only), and only routes whose dependency tree carries a user identity.

The checks are pure functions over plain data so each rule has a synthetic
red-light test next to the real run (``test_route_inventory.py``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from starlette.routing import Mount, Route, WebSocketRoute

ISOLATION_PREFIX = "tests/tenant_isolation/"
INVENTORY_TEST = "tests/tenant_isolation/test_route_inventory.py"
SNAPSHOT_FILE = Path(__file__).with_name("route_probe_snapshot.txt")

# Dependency callables that bind a request to a user (and so to the user's
# selected organization). Signature-checked webhooks carry no such dependency
# and are therefore (b), never (c).
IDENTITY_DEPENDENCIES = frozenset(
    {
        "get_user",
        "get_user_ws",
        "get_user_with_selected_organization",
        "get_superuser",
    }
)

_PARAM = re.compile(r"\[.*\]$")


def route_key(method: str, path: str) -> str:
    return f"{method} {path}"


def _dependency_names(dependant, acc: set[str]) -> set[str]:
    for dep in getattr(dependant, "dependencies", None) or []:
        call = getattr(dep, "call", None)
        if call is not None:
            acc.add(getattr(call, "__name__", repr(call)))
        _dependency_names(dep, acc)
    return acc


def enumerate_routes(app) -> dict[str, frozenset[str]]:
    """Route key -> names of the dependency callables in its dependant tree.

    HTTP routes are keyed per method (``GET /p``), websockets ``WS /p``,
    mounted sub-apps ``MOUNT /p``.
    """
    out: dict[str, frozenset[str]] = {}
    for r in app.routes:
        if isinstance(r, WebSocketRoute):
            keys = [route_key("WS", r.path)]
        elif isinstance(r, Mount):
            keys = [route_key("MOUNT", r.path)]
        elif isinstance(r, Route):
            keys = [route_key(m, r.path) for m in sorted(r.methods or ())]
        else:  # pragma: no cover - starlette has no other route kinds today
            raise TypeError(f"unknown route kind {type(r).__name__}: {r!r}")
        deps = frozenset(_dependency_names(getattr(r, "dependant", None), set()))
        for k in keys:
            out[k] = deps
    return out


def check_classes(
    org_scoped: Mapping[str, Iterable[str]],
    exempt: Mapping[str, str],
    probed: Iterable[str],
) -> list[str]:
    """A route sits in one class only; exemptions carry a reason; (a) cites tests."""
    problems = []
    probed = list(probed)
    seen: dict[str, str] = {}
    for cls, keys in (("a", org_scoped), ("b", exempt), ("c", probed)):
        for k in keys:
            if k in seen:
                problems.append(f"{k}: listed in both ({seen[k]}) and ({cls})")
            seen.setdefault(k, cls)
    if len(probed) != len(set(probed)):
        problems.append("(c) PROBED has duplicate entries")
    for k, reason in exempt.items():
        if not reason or not reason.strip():
            problems.append(f"{k}: (b) exemption without a reason")
    for k, ids in org_scoped.items():
        if not list(ids):
            problems.append(f"{k}: (a) entry cites no isolation test")
    return problems


def check_coverage(
    routes: Iterable[str],
    org_scoped: Mapping[str, Iterable[str]],
    exempt: Mapping[str, str],
    probed: Iterable[str],
) -> list[str]:
    """Every live route is classified; every classified route is live."""
    routes = set(routes)
    classified = set(org_scoped) | set(exempt) | set(probed)
    problems = [f"{k}: unclassified route" for k in sorted(routes - classified)]
    problems += [
        f"{k}: classified but no such route" for k in sorted(classified - routes)
    ]
    return problems


def check_probe_snapshot(probed: Iterable[str], snapshot: Iterable[str]) -> list[str]:
    """(c) only shrinks: entries outside the frozen first snapshot fail."""
    snapshot = set(snapshot)
    return [
        f"{k}: (c) outside the frozen probe snapshot — prove it in (a) or exempt it in (b)"
        for k in probed
        if k not in snapshot
    ]


def check_probe_identity(
    probed: Iterable[str], deps: Mapping[str, Iterable[str]]
) -> list[str]:
    """(c) routes carry a user-identity dependency."""
    return [
        f"{k}: (c) route has no identity dependency ({', '.join(sorted(IDENTITY_DEPENDENCIES))})"
        for k in probed
        if k in deps and not IDENTITY_DEPENDENCIES & set(deps[k])
    ]


def _base(node_id: str) -> str:
    return _PARAM.sub("", node_id)


def check_org_scope_refs(
    org_scoped: Mapping[str, Iterable[str]], collected: Iterable[str]
) -> list[str]:
    """(a) node ids name collected isolation tests (parametrized ids match by base)."""
    collected = set(collected)
    bases = {_base(c) for c in collected}
    problems = []
    for k, ids in org_scoped.items():
        for nid in ids:
            if not nid.startswith(ISOLATION_PREFIX):
                problems.append(f"{k}: {nid} is not under {ISOLATION_PREFIX}")
            elif nid.startswith(INVENTORY_TEST):
                problems.append(f"{k}: {nid} is the inventory test itself")
            elif nid not in collected and nid not in bases:
                problems.append(f"{k}: {nid} not collected by pytest")
    return problems


def referenced_nodes(org_scoped: Mapping[str, Iterable[str]]) -> list[str]:
    return sorted({nid for ids in org_scoped.values() for nid in ids})


def check_org_scope_hits(
    org_scoped: Mapping[str, Iterable[str]], hits: Mapping[str, Iterable[str]]
) -> list[str]:
    """Each cited test actually hit the route (parametrized variants pooled)."""
    pooled: dict[str, set[str]] = {}
    for nid, keys in hits.items():
        pooled.setdefault(nid, set()).update(keys)
        pooled.setdefault(_base(nid), set()).update(keys)
    problems = []
    for k, ids in org_scoped.items():
        for nid in ids:
            if k not in pooled.get(nid, set()):
                problems.append(f"{k}: {nid} ran but never hit this route")
    return problems


def load_snapshot(path: Path = SNAPSHOT_FILE) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
