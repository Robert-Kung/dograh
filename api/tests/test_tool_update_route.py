"""Regression tests for ``PUT /api/v1/tools/{tool_uuid}`` and tool lookup order.

Three defects, one file:

1. ``status: ""`` reached the column having skipped ``validate_status`` — the
   route guarded on truthiness (``if request.status:``) but forwarded the value
   unconditionally, and the client layer persists anything that is not ``None``.
2. ``UpdateToolRequest`` has no ``category`` field, so a PUT could only change
   ``definition.type`` — leaving ``category`` (the call-time dispatch key) and
   the definition describing two different kinds of tool.
3. ``get_tools_by_uuids`` had no ``ORDER BY`` while a caller picks "the first"
   match, so with two matching tools the row order decided the outcome.
"""

from __future__ import annotations

from api.enums import ToolCategory

HTTP_API_DEFINITION = {
    "schema_version": 1,
    "type": "http_api",
    "config": {
        "url": "https://example.com/hook",
        "method": "GET",
        "headers": {},
        "credential_uuid": None,
        "timeout_ms": 5000,
    },
}


async def _user_with_org(db_session, tag: str):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"tool_update_{tag}")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"tool_update_org_{tag}", user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    return await db_session.get_user_by_id(user.id), org


async def _make_tool(db_session, org, user, name: str, category: str, definition: dict):
    return await db_session.create_tool(
        organization_id=org.id,
        user_id=user.id,
        name=name,
        definition=definition,
        category=category,
    )


# ── 1. empty-string status ───────────────────────────────────────────────────


async def test_empty_string_status_is_rejected(test_client_factory, db_session):
    """``status: ""`` must be validated, not treated as a silent no-op.

    The natural reading of the old ``if request.status:`` guard is that an empty
    string does nothing. It did not: the value was passed on unconditionally and
    persisted, so ``""`` was the one value that reached the column having
    bypassed the enum check.
    """
    user, org = await _user_with_org(db_session, "emptystatus")
    tool = await _make_tool(
        db_session, org, user, "Hook", ToolCategory.HTTP_API.value, HTTP_API_DEFINITION
    )

    async with test_client_factory(user) as client:
        response = await client.put(
            f"/api/v1/tools/{tool.tool_uuid}",
            json={"status": ""},
        )

    assert response.status_code == 400, (
        f"Expected 400, got {response.status_code}: {response.text}"
    )


async def test_omitted_status_is_still_a_no_op(test_client_factory, db_session):
    """Guarding on ``is not None`` must not turn an absent status into an error."""
    user, org = await _user_with_org(db_session, "nostatus")
    tool = await _make_tool(
        db_session, org, user, "Hook", ToolCategory.HTTP_API.value, HTTP_API_DEFINITION
    )

    async with test_client_factory(user) as client:
        response = await client.put(
            f"/api/v1/tools/{tool.tool_uuid}",
            json={"name": "Renamed"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"


# ── 2. category / definition.type cross-validation ───────────────────────────


async def test_definition_type_must_match_existing_category(
    test_client_factory, db_session
):
    """A PUT must not leave category and definition.type describing different tools.

    ``category`` is what ``find_transfer_call_config`` dispatches on at call
    time. Writing an ``http_api`` definition onto a ``transfer_call`` tool left
    the runtime still treating it as a transfer while none of
    ``TransferCallConfig``'s validators had run on it.
    """
    user, org = await _user_with_org(db_session, "mismatch")
    tool = await _make_tool(
        db_session,
        org,
        user,
        "Transfer",
        ToolCategory.TRANSFER_CALL.value,
        {
            "schema_version": 1,
            "type": "transfer_call",
            "config": {"destination": "+15551234567"},
        },
    )

    async with test_client_factory(user) as client:
        response = await client.put(
            f"/api/v1/tools/{tool.tool_uuid}",
            json={"definition": HTTP_API_DEFINITION},
        )

    assert response.status_code == 400, (
        f"Expected 400, got {response.status_code}: {response.text}"
    )
    assert "category" in response.json()["detail"]


async def test_matching_definition_type_is_accepted(test_client_factory, db_session):
    """The cross-check must not block an ordinary definition edit."""
    user, org = await _user_with_org(db_session, "match")
    tool = await _make_tool(
        db_session, org, user, "Hook", ToolCategory.HTTP_API.value, HTTP_API_DEFINITION
    )
    edited = {
        **HTTP_API_DEFINITION,
        "config": {**HTTP_API_DEFINITION["config"], "timeout_ms": 9000},
    }

    async with test_client_factory(user) as client:
        response = await client.put(
            f"/api/v1/tools/{tool.tool_uuid}",
            json={"definition": edited},
        )

    assert response.status_code == 200, response.text
    assert response.json()["definition"]["config"]["timeout_ms"] == 9000


# ── 3. stable ordering for "first match" callers ─────────────────────────────


async def test_get_tools_by_uuids_is_ordered(db_session):
    """Two matching tools must come back in a defined order.

    ``find_transfer_call_config`` returns the first ``transfer_call`` match, and
    it passes a ``set`` of uuids, so without an ORDER BY neither the IN-list
    order nor the row order was defined.

    **This one is a guard, not a regression test**: it passes against the
    pre-fix code too, because the database happened to return insertion order.
    Unspecified is not the same as wrong, and a test cannot reliably observe
    "unspecified". It is here so a future change that drops the ORDER BY has
    something to trip over on a backend that orders differently.
    """
    user, org = await _user_with_org(db_session, "ordering")
    first = await _make_tool(
        db_session, org, user, "A", ToolCategory.HTTP_API.value, HTTP_API_DEFINITION
    )
    second = await _make_tool(
        db_session, org, user, "B", ToolCategory.HTTP_API.value, HTTP_API_DEFINITION
    )

    for uuids in (
        [first.tool_uuid, second.tool_uuid],
        [second.tool_uuid, first.tool_uuid],
    ):
        tools = await db_session.get_tools_by_uuids(uuids, org.id)
        assert [t.id for t in tools] == sorted(t.id for t in tools)
        assert [t.tool_uuid for t in tools] == [first.tool_uuid, second.tool_uuid]
