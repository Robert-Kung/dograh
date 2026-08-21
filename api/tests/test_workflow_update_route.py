"""Regression tests for ``PUT /api/v1/workflow/{id}``.

Covers the name-only update path, which has no versioned changes and therefore
never re-fetches the workflow inside ``update_workflow``. Before the fix it
returned the detached instance from the first query — which did not eager-load
``released_definition`` — and the route's response assembly lazy-loaded it,
raising ``DetachedInstanceError`` that the handler's trailing ``except
Exception`` folded into a 500.

The bug is state-dependent: it only reproduces while the workflow has **no
draft**, because ``get_draft_version`` returning a row short-circuits the
failing branch. A freshly created workflow is exactly that state
(``create_workflow`` writes V1 as published).
"""

from __future__ import annotations

WORKFLOW_DEFINITION = {"nodes": [], "edges": [], "viewport": {}}


async def _user_with_org(db_session, tag: str):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"wf_update_{tag}")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"wf_update_org_{tag}", user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    return await db_session.get_user_by_id(user.id), org


async def test_name_only_update_on_published_workflow_without_draft(
    test_client_factory, db_session
):
    """A name-only PUT must return 200 when the workflow has no draft."""
    user, org = await _user_with_org(db_session, "nodraft")
    workflow = await db_session.create_workflow(
        name="Before",
        workflow_definition=WORKFLOW_DEFINITION,
        user_id=user.id,
        organization_id=org.id,
    )

    async with test_client_factory(user) as client:
        response = await client.put(
            f"/api/v1/workflow/{workflow.id}",
            json={"name": "After"},
        )

    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}: {response.text}"
    )
    assert response.json()["name"] == "After"


async def test_name_only_update_after_a_draft_exists(test_client_factory, db_session):
    """The same request on the other side of the state split still works.

    This is the case that always passed: once a save has created a draft,
    ``get_draft_version`` returns a row and the lazy-load branch is never
    reached. Kept so a future change cannot fix one state by breaking the other.
    """
    user, org = await _user_with_org(db_session, "withdraft")
    workflow = await db_session.create_workflow(
        name="Before",
        workflow_definition=WORKFLOW_DEFINITION,
        user_id=user.id,
        organization_id=org.id,
    )
    await db_session.save_workflow_draft(
        workflow_id=workflow.id,
        workflow_definition={"nodes": [], "edges": [], "viewport": {"zoom": 1}},
        workflow_configurations=None,
        template_context_variables=None,
    )

    async with test_client_factory(user) as client:
        response = await client.put(
            f"/api/v1/workflow/{workflow.id}",
            json={"name": "After"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "After"
