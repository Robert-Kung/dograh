"""DB-row-shaped workflow fakes for tests."""

import types


def workflow_row(*, tool_uuid: str | None = "xfer-1", organization_id: int = 1):
    """A ``WorkflowModel``-shaped row: the definition carries JSON, no ``nodes``.

    Shaped like what ``db_client.get_workflow`` returns, so code that needs a
    node graph has to build one (answer-before-refer review H1: a fake with
    ``.nodes`` hid that the overflow gate passed the row itself)."""
    from api.services.workflow.dto import (
        EdgeDataDTO,
        EndCallNodeData,
        Position,
        ReactFlowDTO,
        RFEdgeDTO,
        RFNodeDTO,
        StartCallNodeData,
    )

    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start",
                    prompt="hi",
                    is_start=True,
                    tool_uuids=[tool_uuid] if tool_uuid else None,
                ),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=200),
                data=EndCallNodeData(name="End", prompt="bye", is_end=True),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="e",
                source="start",
                target="end",
                data=EdgeDataDTO(label="End", condition="end"),
            )
        ],
    )
    definition = types.SimpleNamespace(workflow_json=dto.model_dump(mode="json"))
    return types.SimpleNamespace(
        id=1,
        organization_id=organization_id,
        released_definition=definition,
        current_definition=None,
        workflow_definition={},
    )
