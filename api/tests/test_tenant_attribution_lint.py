"""Static check: attribution by the row's organization (mt-s1-foundation 4.10).

tenant-isolation〈歸屬以資料列的組織判定〉 / design D10. AST-scans ``api/db/``
and ``api/routes/`` and fails on:

① ``UserModel.selected_organization_id`` used as a workflow / run attribution
   condition (a comparison, or ``.has(selected_organization_id=...)``);
② an ``organization_id`` truthiness / ``is not None`` branch that adds a query
   condition — with no ``else`` (unfiltered when missing) or an ``elif`` /
   ``else`` falling back to ``user_id``;
③ an ``is_superuser`` branch outside the superuser routes.

Each exemption names its reason. ``scan(root)`` takes the tree root so the
check can be proven red against pre-fix sources.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("db", "routes")

# (relative path, enclosing function) -> reason
EXEMPT_ORG_BRANCH = {
    (
        "db/telephony_phone_number_client.py",
        "find_inbound_route_by_account",
    ): (
        "inbound provider webhook: the organization is not known yet and is "
        "derived from the matched number; the optional org only narrows it"
    ),
}
EXEMPT_SUPERUSER = {
    ("routes/s3_signed_url.py", "_authorize_and_get_workflow_run"): (
        "superuser may sign any run's object (existing branch); non-superusers "
        "get the run only through get_workflow_run scoped to their organization"
    ),
    ("routes/s3_signed_url.py", "get_signed_url"): (
        "non-superuser may only sign keys under their own organization — the "
        "branch IS the organization restriction; removing it widens access"
    ),
}
SUPERUSER_FILES = {"routes/superuser.py"}


def _files(root: Path):
    for sub in SCANNED:
        for path in sorted((root / sub).rglob("*.py")):
            yield path.relative_to(root).as_posix(), ast.parse(path.read_text())


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _tests_org_presence(test) -> bool:
    """``if organization_id:`` / ``if organization_id is not None:``."""
    if isinstance(test, ast.Name) and test.id == "organization_id":
        return True
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "organization_id"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    )


def _adds_condition(stmts) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"where", "filter", "filter_by"}
        for s in stmts
        for n in ast.walk(s)
    )


def _is_column_selected_org(node) -> bool:
    """``UserModel.selected_organization_id`` — the column in a query, as
    opposed to ``user.selected_organization_id`` (the caller's own current
    organization, an authorization input that D10 leaves out of scope)."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "selected_organization_id"
        and isinstance(node.value, ast.Name)
        and node.value.id == "UserModel"
    )


def _selected_org_conditions(rel, tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            _is_column_selected_org(n) for n in ast.walk(node)
        ):
            yield f"{rel}:{node.lineno} ① selected_organization_id in a condition"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "has"
            and any(k.arg == "selected_organization_id" for k in node.keywords)
        ):
            yield f"{rel}:{node.lineno} ① .has(selected_organization_id=...)"


def _org_fallback_branches(rel, tree):
    for fn in _functions(tree):
        if (rel, fn.name) in EXEMPT_ORG_BRANCH:
            continue
        for node in ast.walk(fn):
            if not (
                isinstance(node, ast.If)
                and _tests_org_presence(node.test)
                and _adds_condition(node.body)
            ):
                continue
            if not node.orelse:
                yield f"{rel}:{node.lineno} ② {fn.name}: unfiltered when org missing"
            elif "user_id" in _names(ast.Module(body=node.orelse, type_ignores=[])):
                yield f"{rel}:{node.lineno} ② {fn.name}: falls back to user_id"


def _superuser_branches(rel, tree):
    if rel in SUPERUSER_FILES:
        return
    for fn in _functions(tree):
        if (rel, fn.name) in EXEMPT_SUPERUSER:
            continue
        for node in ast.walk(fn):
            if isinstance(node, (ast.If, ast.IfExp)) and "is_superuser" in _names(
                node.test
            ):
                yield f"{rel}:{node.lineno} ③ {fn.name}: is_superuser branch"


def scan(root: Path = API_ROOT) -> list[str]:
    out: list[str] = []
    for rel, tree in _files(root):
        out += _selected_org_conditions(rel, tree)
        out += _org_fallback_branches(rel, tree)
        out += _superuser_branches(rel, tree)
    return out


def test_no_attribution_violations():
    assert scan() == []


def test_exemptions_point_at_real_functions():
    """An exemption whose function was renamed or removed must not linger."""
    have = {(rel, fn.name) for rel, tree in _files(API_ROOT) for fn in _functions(tree)}
    for key in (*EXEMPT_ORG_BRANCH, *EXEMPT_SUPERUSER):
        assert key in have, key


@pytest.mark.parametrize(
    "src, mark",
    [
        (
            "async def f(organization_id=None, user_id=None):\n"
            "    if organization_id:\n"
            "        q = q.where(W.organization_id == organization_id)\n"
            "    elif user_id:\n"
            "        q = q.where(W.user_id == user_id)\n",
            "②",
        ),
        (
            "async def f(organization_id=None):\n"
            "    if organization_id:\n"
            "        q = q.where(W.organization_id == organization_id)\n",
            "②",
        ),
        (
            "async def f(organization_id):\n"
            "    q = q.where(UserModel.selected_organization_id == organization_id)\n",
            "①",
        ),
        (
            "async def f(organization_id):\n"
            "    q = q.where(W.user.has(selected_organization_id=organization_id))\n",
            "①",
        ),
        ("def f(user):\n    if user.is_superuser:\n        pass\n", "③"),
    ],
)
def test_detects_each_shape(tmp_path, src, mark):
    for sub in SCANNED:
        (tmp_path / sub).mkdir()
    (tmp_path / "db" / "x.py").write_text(src)
    found = scan(tmp_path)
    assert found and all(mark in f for f in found), found


async def test_tenant_members_are_not_superusers(db_session):
    """S5: CCP tenant members are plain users."""
    from api.db.models import UserModel

    user, _ = await db_session.get_or_create_user_by_provider_id("lint_member")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        "lint_org", user.id
    )
    await db_session.add_user_to_organization(user.id, org.id)
    assert UserModel.is_superuser.default.arg is False
    assert (await db_session.get_user_by_id(user.id)).is_superuser is False
