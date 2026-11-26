"""Static check: attribution by the row's organization (mt-s1-foundation 4.10).

tenant-isolation〈歸屬以資料列的組織判定〉 / design D10. AST-scans ``api/db/``
and ``api/routes/`` and fails on:

① ``UserModel.selected_organization_id`` used as a workflow / run attribution
   condition (a comparison, or ``.has(selected_organization_id=...)``);
② an ``organization_id`` truthiness / ``is not None`` branch that adds a query
   condition — with no ``else`` (unfiltered when missing) or an ``elif`` /
   ``else`` falling back to ``user_id``;
③ an ``is_superuser`` branch outside the superuser routes;
④ a function in ``api/routes/`` passing a request-sourced identifier (any
   parameter that is not a ``Depends``) to a ``db_client`` method whose
   signature has no ``organization_id`` — unless an ownership check by
   ``selected_organization_id`` dominates the call: the call sits inside an
   ``if`` testing an organization-scoped value, or an earlier statement on its
   path is an ``if`` testing one whose body raises. Judged by signature, not by
   the db function's name (mt-s1-foundation 8.3c).

Each exemption names its reason. ``scan(root)`` takes the tree root so the
check can be proven red against pre-fix sources.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
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
        "get the run only through get_workflow_run scoped to their organization "
        "and a miss always raises 403 — the former require_workflow_run=False "
        "path (file-metadata) did not raise and leaked other organizations' "
        "metadata; the parameter is removed (8.3b, R1)"
    ),
    ("routes/s3_signed_url.py", "get_file_metadata"): (
        "voicemail_detections/ keys carry no run or organization — the branch "
        "restricts them to superusers (403 otherwise); removing it widens access"
    ),
    ("routes/s3_signed_url.py", "get_signed_url"): (
        "non-superuser may only sign keys under their own organization — the "
        "branch IS the organization restriction; removing it widens access"
    ),
}
SUPERUSER_FILES = {"routes/superuser.py"}

_NO_IDENTITY = (
    "無使用者身分的路由（(b) 豁免），識別碼即憑證（不可猜 token／API key）或承載商回呼"
)
_CARRIER = "承載商路由（(b) 豁免），組織取自號碼／設定列；websocket 為殘留風險 R-MT-11"
# (relative path, enclosing function, db function) -> reason
EXEMPT_UNSCOPED_CALL = {
    (
        "routes/agent_stream.py",
        "agent_stream_websocket",
        "get_workflow_by_uuid_unscoped",
    ): _NO_IDENTITY,
    ("routes/public_agent.py", "_validate_api_key", "validate_api_key"): _NO_IDENTITY,
    (
        "routes/public_agent.py",
        "_get_workflow_definition_for_execution",
        "get_draft_version",
    ): ("workflow 取自已驗 API key 所屬組織的 trigger；" + _NO_IDENTITY),
    (
        "routes/public_agent.py",
        "_resolve_trigger_target",
        "get_agent_trigger_by_path",
    ): _NO_IDENTITY,
    (
        "routes/public_download.py",
        "download_workflow_artifact",
        "get_workflow_run_by_public_token",
    ): _NO_IDENTITY,
    (
        "routes/public_embed.py",
        "_config_preflight_response",
        "get_embed_token_by_token",
    ): _NO_IDENTITY,
    (
        "routes/public_embed.py",
        "_turn_credentials_preflight_response",
        "get_embed_session_by_token",
    ): _NO_IDENTITY,
    (
        "routes/public_embed.py",
        "initialize_embed_session",
        "get_embed_token_by_token",
    ): _NO_IDENTITY,
    (
        "routes/public_embed.py",
        "get_embed_config",
        "get_embed_token_by_token",
    ): _NO_IDENTITY,
    (
        "routes/public_embed.py",
        "get_public_turn_credentials",
        "get_embed_session_by_token",
    ): _NO_IDENTITY,
    (
        "routes/webrtc_signaling.py",
        "public_signaling_websocket",
        "get_embed_session_by_token",
    ): _NO_IDENTITY,
    (
        "routes/telephony.py",
        "_verify_organization_phone_number",
        "list_phone_numbers_for_config",
    ): _CARRIER,
    (
        "routes/telephony.py",
        "_validate_inbound_request",
        "get_workflow_by_id",
    ): _CARRIER,
    (
        "routes/telephony.py",
        "_handle_telephony_websocket",
        "get_workflow_run_by_id",
    ): _CARRIER,
    (
        "routes/telephony.py",
        "_handle_telephony_websocket",
        "update_workflow_run",
    ): _CARRIER,
    (
        "routes/campaign.py",
        "_get_campaign_stats",
        "get_queued_runs_stats_for_campaigns",
    ): ("helper：每個呼叫點傳入的都是已以 organization_id 取回的 campaign.id"),
    (
        "routes/workflow.py",
        "_validate_workflow_definition",
        "check_trigger_path_conflicts",
    ): (
        "trigger path 全平台唯一，衝突檢查刻意跨組織、只回傳呼叫端自己送出的 path；"
        "exclude_workflow_id 由呼叫點以組織範圍取回的 workflow 提供"
    ),
    (
        "routes/s3_signed_url.py",
        "_authorize_and_get_workflow_run",
        "get_workflow_run_by_id",
    ): (
        "superuser 分支（③ 已豁免同函式）；非 superuser 只經組織範圍 get_workflow_run，查無即 403"
    ),
    ("routes/auth.py", "signup", "get_user_by_email"): (
        "使用者帳號（email）全平台唯一、非組織範圍資源；signup 受部署層密鑰保護"
    ),
    ("routes/auth.py", "signup", "create_user_with_email"): (
        "同上：建立使用者本身不屬任何組織，組織於其後以新使用者建立"
    ),
    ("routes/auth.py", "login", "get_user_by_email"): (
        "登入以 email 查全平台唯一帳號，回應前驗密碼；非組織範圍資源"
    ),
    ("routes/webrtc_signaling.py", "_handle_offer", "get_workflow_organization_id"): (
        "呼叫端 signaling_websocket 先以組織範圍取 run，並拒絕與 run 不符的 workflow_id"
    ),
}
# Parameters that are never request-sourced identifiers.  ``request`` is not
# here: many routes name their Pydantic body ``request``; only a parameter
# annotated as the Starlette ``Request``／``WebSocket`` object is skipped.
_NOT_REQUEST_PARAMS = {
    "self",
    "cls",
    "user",
    "websocket",
    "ws",
    "background_tasks",
}
_FRAMEWORK_TYPES = {"Request", "WebSocket", "BackgroundTasks"}
# Route helpers that raise unless the id belongs to the organization passed
# alongside it.  A call to one, with the request id and an organization-scoped
# value, guards later calls on that id.
OWNERSHIP_HELPERS = {
    ("routes/organization.py", "_ensure_config_belongs_to_org"): (
        "以 organization_id 組織範圍查 telephony 設定，查無即 404"
    ),
    ("routes/organization.py", "_ensure_workflow_belongs_to_org"): (
        "以 organization_id 組織範圍查工作流，查無即 404"
    ),
}


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


def _db_signatures(root: Path) -> dict[str, bool]:
    """db function name -> whether its signature takes ``organization_id``.

    A name defined more than once counts as unscoped if any definition lacks
    the parameter (the call could resolve to it)."""
    sig: dict[str, bool] = {}
    for path in sorted((root / "db").rglob("*.py")):
        for fn in _functions(ast.parse(path.read_text())):
            a = fn.args
            takes = "organization_id" in {
                x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)
            }
            sig[fn.name] = sig.get(fn.name, True) and takes
    return sig


def _is_depends(node) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "Depends"
        for n in ast.walk(node)
    )


def _request_params(fn) -> set[str]:
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    pairs = [*zip(positional, defaults), *zip(a.kwonlyargs, a.kw_defaults)]
    return {
        p.arg
        for p, d in pairs
        if p.arg not in _NOT_REQUEST_PARAMS
        and not (d is not None and _is_depends(d))
        and not (p.annotation is not None and _is_depends(p.annotation))
        and not (p.annotation is not None and _names(p.annotation) & _FRAMEWORK_TYPES)
    }


def _scoped_names(fn) -> set[str]:
    """Names bound (transitively) from an expression using ``selected_organization_id``."""
    scoped: set[str] = set()
    changed = True
    while changed:
        changed = False
        for st in ast.walk(fn):
            if not isinstance(st, (ast.Assign, ast.AnnAssign)) or st.value is None:
                continue
            if not ({"selected_organization_id"} | scoped) & _names(st.value):
                continue
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name) and n.id not in scoped:
                        scoped.add(n.id)
                        changed = True
    return scoped


def _org_aliases(fn) -> set[str]:
    """Names bound to the bare ``selected_organization_id`` value (not to a
    row fetched with it)."""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for st in ast.walk(fn):
            if not isinstance(st, ast.Assign):
                continue
            v = st.value
            if (
                isinstance(v, ast.Attribute) and v.attr == "selected_organization_id"
            ) or (isinstance(v, ast.Name) and v.id in aliases):
                for t in st.targets:
                    if isinstance(t, ast.Name) and t.id not in aliases:
                        aliases.add(t.id)
                        changed = True
    return aliases


def _is_presence_check(test, aliases: set[str]) -> bool:
    """``if not user.selected_organization_id:`` only proves an organization
    is selected, not that the id belongs to it.  (``if workflow is None`` on a
    row fetched with the organization is an ownership check.)"""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
    elif (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        test = test.left
    else:
        return False
    if isinstance(test, ast.Attribute):
        return test.attr == "selected_organization_id"
    return isinstance(test, ast.Name) and test.id in aliases


def _ownership_helper_call(st, org: set[str], ids: set[str]) -> bool:
    """``await helper(id, org)`` as a statement of its own (bare or assigned).

    Only an unconditional call at this block level counts: a helper inside an
    ``if``/``try``/loop/lambda may not run, and a same-named attribute
    (``mod.helper``) may be another function.  Known limit: one validated
    request id is enough even if the db call takes others (the phone-number
    queries also filter by the validated config id; a stricter rule flags
    those safe calls) — review gate round 2 S2.
    """
    helpers = {name for _, name in OWNERSHIP_HELPERS}
    if not isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign)) or st.value is None:
        return False
    n = st.value.value if isinstance(st.value, ast.Await) else st.value
    if not (
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in helpers
    ):
        return False
    args = [*n.args, *(k.value for k in n.keywords)]
    names = set().union(*(_names(a) for a in args)) if args else set()
    return bool(names & org and names & ids)


def _dominating_guard(fn, call, scoped: set[str], ids: set[str]) -> bool:
    """An ownership check on every path to ``call`` (see ④ above)."""
    org = {"selected_organization_id"} | scoped
    aliases = _org_aliases(fn)

    def visit(stmts) -> bool | None:
        # True: guarded; False: reached unguarded; None: not in this block.
        guarded = False
        for st in stmts:
            if call in set(ast.walk(st)):
                if (
                    isinstance(st, ast.If)
                    and org & _names(st.test)
                    and not _is_presence_check(st.test, aliases)
                ):
                    in_body = any(call in set(ast.walk(b)) for b in st.body)
                    if in_body:
                        return True
                for field in ("body", "orelse", "finalbody", "handlers"):
                    inner = getattr(st, field, None)
                    if isinstance(inner, list) and inner:
                        r = visit(
                            [x for h in inner for x in h.body]
                            if field == "handlers"
                            else inner
                        )
                        if r is not None:
                            return guarded or r
                return guarded
            if (
                isinstance(st, ast.If)
                and org & _names(st.test)
                and not _is_presence_check(st.test, aliases)
                and any(isinstance(n, ast.Raise) for b in st.body for n in ast.walk(b))
            ) or _ownership_helper_call(st, org, ids):
                guarded = True
        return None

    return bool(visit(fn.body))


def _unscoped_calls(rel, tree, signatures):
    if not rel.startswith("routes/") or rel in SUPERUSER_FILES:
        return
    for fn in _functions(tree):
        params = _request_params(fn)
        if not params:
            continue
        scoped = _scoped_names(fn)
        for call in ast.walk(fn):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "db_client"
                and signatures.get(call.func.attr) is False
            ):
                continue
            args = [*call.args, *(k.value for k in call.keywords)]
            ids = params & set().union(*(_names(a) for a in args)) if args else set()
            if not ids:
                continue
            if (rel, fn.name, call.func.attr) in EXEMPT_UNSCOPED_CALL:
                continue
            if _dominating_guard(fn, call, scoped, ids):
                continue
            yield (
                f"{rel}:{call.lineno} ④ {fn.name}: request id -> "
                f"db_client.{call.func.attr} (no organization_id, no ownership check)"
            )


def scan(root: Path = API_ROOT) -> list[str]:
    out: list[str] = []
    signatures = _db_signatures(root)
    for rel, tree in _files(root):
        out += _selected_org_conditions(rel, tree)
        out += _org_fallback_branches(rel, tree)
        out += _superuser_branches(rel, tree)
        out += _unscoped_calls(rel, tree, signatures)
    return out


@pytest.mark.scenario("tenant-isolation:靜態檢查")
def test_no_attribution_violations():
    assert scan() == []


def test_exemptions_point_at_real_functions():
    """An exemption whose function was renamed or removed must not linger."""
    have = {(rel, fn.name) for rel, tree in _files(API_ROOT) for fn in _functions(tree)}
    for key in (*EXEMPT_ORG_BRANCH, *EXEMPT_SUPERUSER, *OWNERSHIP_HELPERS):
        assert key in have, key
    signatures = _db_signatures(API_ROOT)
    for rel, fn, db_fn in EXEMPT_UNSCOPED_CALL:
        assert (rel, fn) in have, (rel, fn)
        assert signatures.get(db_fn) is False, (rel, fn, db_fn)


@pytest.mark.scenario("tenant-isolation:靜態檢查")
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


_DB_STUB = (
    "async def get_thing(thing_id): ...\n"
    "async def get_thing_scoped(thing_id): ...\n"
    "async def get_org_thing(thing_id, organization_id): ...\n"
)
_HEAD = "async def h(thing_id: int, user=Depends(get_user)):\n"
_OWN = "    t = await db_client.get_org_thing(thing_id, organization_id=user.selected_organization_id)\n"


@pytest.mark.scenario("tenant-isolation:先查後做缺歸屬檢查")
@pytest.mark.parametrize(
    "body, flagged",
    [
        ("    return await db_client.get_thing(thing_id)\n", True),
        # judged by signature: a reassuring name does not help
        ("    return await db_client.get_thing_scoped(thing_id)\n", True),
        (
            _OWN
            + "    if not t:\n        raise E(404)\n    return await db_client.get_thing(thing_id)\n",
            False,
        ),
        (
            _OWN + "    if t:\n        return await db_client.get_thing(thing_id)\n",
            False,
        ),
        # a check that does not raise is not a check
        (
            _OWN
            + "    if not t:\n        log()\n    return await db_client.get_thing(thing_id)\n",
            True,
        ),
        # a check on another branch does not dominate the call
        (
            "    if x:\n"
            + "    "
            + _OWN
            + "        if not t:\n            raise E(404)\n"
            "    else:\n        return await db_client.get_thing(thing_id)\n",
            True,
        ),
        (
            "    return await db_client.get_org_thing(thing_id, organization_id=1)\n",
            False,
        ),
        # only proves an organization is selected, not that thing_id is in it
        (
            "    if not user.selected_organization_id:\n        raise E(400)\n"
            "    return await db_client.get_thing(thing_id)\n",
            True,
        ),
        (
            "    org = user.selected_organization_id\n"
            "    if org is None:\n        raise E(400)\n"
            "    return await db_client.get_thing(thing_id)\n",
            True,
        ),
        (
            "    await _ensure_config_belongs_to_org(thing_id, user.selected_organization_id)\n"
            "    return await db_client.get_thing(thing_id)\n",
            False,
        ),
        # the helper must be given the id being used
        (
            "    await _ensure_config_belongs_to_org(other_id, user.selected_organization_id)\n"
            "    return await db_client.get_thing(thing_id)\n",
            True,
        ),
        # only an unconditional, top-level, bare-name call guards
        (
            "    if flag:\n"
            "        await _ensure_config_belongs_to_org(thing_id, user.selected_organization_id)\n"
            "    return await db_client.get_thing(thing_id)\n",
            True,
        ),
        (
            "    try:\n"
            "        await _ensure_config_belongs_to_org(thing_id, user.selected_organization_id)\n"
            "    except Exception:\n        pass\n"
            "    return await db_client.get_thing(thing_id)\n",
            True,
        ),
        (
            "    await mymod._ensure_config_belongs_to_org(thing_id, user.selected_organization_id)\n"
            "    return await db_client.get_thing(thing_id)\n",
            True,
        ),
        (
            "    f = lambda: _ensure_config_belongs_to_org(thing_id, user.selected_organization_id)\n"
            "    return await db_client.get_thing(thing_id)\n",
            True,
        ),
    ],
    ids=[
        "unguarded",
        "name-not-signature",
        "raise-guard",
        "if-body",
        "no-raise",
        "sibling-branch",
        "takes-org",
        "presence-only",
        "presence-alias",
        "ownership-helper",
        "helper-other-id",
        "helper-in-if",
        "helper-in-try",
        "helper-attribute",
        "helper-in-lambda",
    ],
)
def test_detects_unscoped_call_shape(tmp_path, body, flagged):
    for sub in SCANNED:
        (tmp_path / sub).mkdir()
    (tmp_path / "db" / "c.py").write_text(_DB_STUB)
    (tmp_path / "routes" / "r.py").write_text(_HEAD + body)
    found = [f for f in scan(tmp_path) if "④" in f]
    assert bool(found) == flagged, found


def test_dependency_params_are_not_request_ids(tmp_path):
    for sub in SCANNED:
        (tmp_path / sub).mkdir()
    (tmp_path / "db" / "c.py").write_text(_DB_STUB)
    (tmp_path / "routes" / "r.py").write_text(
        "async def h(user=Depends(get_user)):\n    return await db_client.get_thing(user.id)\n"
    )
    assert scan(tmp_path) == []


@pytest.mark.parametrize(
    "head, flagged",
    [
        # a Pydantic body named ``request`` carries request ids
        ("async def h(request: Body, user=Depends(get_user)):\n", True),
        ("async def h(request: Request, user=Depends(get_user)):\n", False),
    ],
    ids=["body-named-request", "starlette-request"],
)
def test_request_param_by_annotation(tmp_path, head, flagged):
    for sub in SCANNED:
        (tmp_path / sub).mkdir()
    (tmp_path / "db" / "c.py").write_text(_DB_STUB)
    (tmp_path / "routes" / "r.py").write_text(
        head + "    return await db_client.get_thing(request.thing_id)\n"
    )
    assert bool([f for f in scan(tmp_path) if "④" in f]) == flagged
