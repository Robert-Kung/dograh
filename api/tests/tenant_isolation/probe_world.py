"""Two-tenant world and fakes for the cross-org route probe (mt-s1-foundation 8.3a).

Builds on ``two_tenants``: A and B each get one of every resource the (c)
routes touch, named with a per-tenant token so "the response holds none of
A's identifiers" is a substring check on tokens and uuids, never on bare
integer ids. External systems (object storage, MPS, the arq queue, worker
sync, embeddings, CSV download, WebRTC signaling) are replaced by in-memory
fakes that keep per-owner data, so a route that forwards the wrong
organization gets the other tenant's data back and the probe sees it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from api.db.models import KnowledgeBaseChunkModel
from api.enums import OrganizationConfigurationKey
from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.schemas.organization_preferences import OrganizationPreferences
from api.services.configuration.registry import DograhLLMService
from api.services.organization_preferences import upsert_organization_preferences
from api.services.workflow.text_chat_session_service import (
    default_text_chat_checkpoint,
    default_text_chat_session_data,
)

TOKENS = {"a": "PRB7A-q9x", "b": "PRB7B-k4m"}
TEST_PHONES = {"a": "+886900007001", "b": "+886900007002"}
NEW_DID = "+886221119999"
EMBED_DIM = 1536


@dataclass
class ProbeTenant:
    tag: str
    token: str
    org_id: int
    user: object
    workflow: object
    run: object
    number: object
    config_id: int
    folder: object
    campaign: object
    credential: object
    tool: object
    document: object
    doc_key: str
    recording: object
    embed: object
    api_key: object
    text_run: object
    service_key_id: str
    csv_key: str
    markers: list[str] = field(default_factory=list)


@dataclass
class ProbeWorld:
    a: ProbeTenant
    b: ProbeTenant


async def _make(db, session, t) -> ProbeTenant:
    tag, org_id, user = t.tag, t.organization_id, t.user
    token = TOKENS[tag]
    t.org.price_per_second_usd = 0.01
    t.workflow.name = f"{token} wf"
    t.run.name = f"{token} run"
    await session.flush()
    cfg_id = t.number.telephony_configuration_id
    await session.execute(
        text("UPDATE telephony_configurations SET name=:n WHERE id=:i"),
        {"n": f"{token} cfg", "i": cfg_id},
    )

    folder = await db.create_folder(f"{token} folder", org_id)
    csv_key = f"campaigns/{org_id}/{token}.csv"
    campaign = await db.create_campaign(
        name=f"{token} campaign",
        workflow_id=t.workflow.id,
        source_type="csv",
        source_id=csv_key,
        user_id=user.id,
        organization_id=org_id,
        telephony_configuration_id=cfg_id,
    )
    t.run.campaign_id = campaign.id
    await session.flush()

    credential = await db.create_credential(
        organization_id=org_id,
        user_id=user.id,
        name=f"{token} cred",
        credential_type="api_key",
        credential_data={"header_name": "X-Key", "api_key": f"secret-{token}"},
    )
    tool = await db.create_tool(
        organization_id=org_id,
        user_id=user.id,
        name=f"{token} tool",
        definition={
            "schema_version": 1,
            "type": "http_api",
            "config": {
                "method": "POST",
                "url": "https://example.com/hook",
                "credential_uuid": credential.credential_uuid,
            },
        },
    )

    doc_uuid = str(uuid.uuid4())
    doc_key = f"knowledge_base/{org_id}/{doc_uuid}/{token}.pdf"
    document = await db.create_document(
        organization_id=org_id,
        created_by=user.id,
        filename=f"{token}.pdf",
        file_size_bytes=1,
        file_hash=f"hash-{token}",
        mime_type="application/pdf",
        custom_metadata={"s3_key": doc_key},
        document_uuid=doc_uuid,
    )
    session.add(
        KnowledgeBaseChunkModel(
            document_id=document.id,
            organization_id=org_id,
            chunk_text=f"{token} chunk",
            chunk_index=0,
            chunk_metadata={},
            embedding_model="probe",
            embedding_dimension=EMBED_DIM,
            embedding=[0.1] * EMBED_DIM,
        )
    )
    await session.flush()

    rec_id = f"{token}-rec"
    recording = await db.create_recording(
        recording_id=rec_id,
        organization_id=org_id,
        transcript=f"{token} transcript",
        storage_key=f"recordings/{org_id}/{rec_id}/{token}.wav",
        storage_backend="minio",
        created_by=user.id,
        workflow_id=t.workflow.id,
    )
    embed = await db.create_embed_token(
        workflow_id=t.workflow.id, organization_id=org_id, created_by=user.id
    )
    api_key, _ = await db.create_api_key(org_id, f"{token} key", user.id)

    text_run = await db.create_workflow_run(
        f"{token} chat",
        t.workflow.id,
        "textchat",
        user.id,
        organization_id=org_id,
    )
    await db.ensure_workflow_run_text_session(
        text_run.id,
        session_data=default_text_chat_session_data(),
        checkpoint=default_text_chat_checkpoint(),
    )

    await db.upsert_configuration(
        org_id,
        OrganizationConfigurationKey.LANGFUSE_CREDENTIALS.value,
        {
            "host": f"https://{token}.langfuse.example",
            "public_key": f"pk-{token}",
            "secret_key": f"sk-{token}",
        },
    )
    # Legacy per-user model configuration (what migration-preview/migrate read).
    await db.update_user_configuration(
        user.id,
        EffectiveAIModelConfiguration(
            llm=DograhLLMService(api_key=f"mps-{token}-0123456789abcdef")
        ),
    )
    await upsert_organization_preferences(
        org_id,
        OrganizationPreferences(test_phone_number=TEST_PHONES[tag], timezone="UTC"),
    )

    pt = ProbeTenant(
        tag=tag,
        token=token,
        org_id=org_id,
        user=user,
        workflow=t.workflow,
        run=t.run,
        number=t.number,
        config_id=cfg_id,
        folder=folder,
        campaign=campaign,
        credential=credential,
        tool=tool,
        document=document,
        doc_key=doc_key,
        recording=recording,
        embed=embed,
        api_key=api_key,
        text_run=text_run,
        service_key_id=f"svc-{token}",
        csv_key=csv_key,
    )
    pt.markers = [
        token,
        t.workflow.workflow_uuid,
        credential.credential_uuid,
        tool.tool_uuid,
        doc_uuid,
        doc_key,
        recording.storage_key,
        embed.token,
        api_key.key_prefix,
        t.number.address,
        TEST_PHONES[tag],
        csv_key,
        user.provider_id,
        t.org.provider_id,
    ]
    return pt


async def build_world(db, session, two) -> ProbeWorld:
    a = await _make(db, session, two.a)
    b = await _make(db, session, two.b)
    return ProbeWorld(a=a, b=b)


# --- snapshot of A's rows ------------------------------------------------------
# A probe is also a write probe: after B's request every row A owns (or that
# points at A's workflows) SHALL be byte-identical.

_OWNED_BY_WORKFLOW = {
    "workflow_definitions": "workflow_id",
    "workflow_runs": "workflow_id",
    "campaigns": "workflow_id",
    "telephony_phone_numbers": "inbound_workflow_id",
    "embed_tokens": "workflow_id",
    "workflow_recordings": "workflow_id",
    "agent_triggers": "workflow_id",
}


async def snapshot_org(session, org_id: int) -> dict[str, list]:
    tables = (
        await session.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name='organization_id' "
                "ORDER BY table_name"
            )
        )
    ).scalars()
    out: dict[str, list] = {}
    for table in list(tables):
        rows = await session.execute(
            text(
                f'SELECT to_jsonb(t)::text FROM "{table}" t '
                "WHERE t.organization_id = :o ORDER BY 1"
            ),
            {"o": org_id},
        )
        out[table] = list(rows.scalars())
    for table, col in _OWNED_BY_WORKFLOW.items():
        rows = await session.execute(
            text(
                f'SELECT to_jsonb(t)::text FROM "{table}" t WHERE t."{col}" IN '
                "(SELECT id FROM workflows WHERE organization_id = :o) ORDER BY 1"
            ),
            {"o": org_id},
        )
        out[f"{table}<-workflow"] = list(rows.scalars())
    for table, join in (
        ("workflow_run_text_sessions", "workflow_run_id"),
        ("ccp_call_meta", "workflow_run_id"),
    ):
        rows = await session.execute(
            text(
                f'SELECT to_jsonb(t)::text FROM "{table}" t WHERE t."{join}" IN '
                "(SELECT r.id FROM workflow_runs r JOIN workflows w ON w.id = r.workflow_id "
                "WHERE w.organization_id = :o) ORDER BY 1"
            ),
            {"o": org_id},
        )
        out[f"{table}<-run"] = list(rows.scalars())
    out["organizations"] = list(
        (
            await session.execute(
                text("SELECT to_jsonb(t)::text FROM organizations t WHERE id = :o"),
                {"o": org_id},
            )
        ).scalars()
    )
    return out


# --- fakes for external systems -----------------------------------------------


class FakeStorage:
    """Presign/sign echo the key so a cross-org key in a response is visible."""

    async def aget_presigned_put_url(self, file_path, **_):
        return f"https://storage.fake/put/{file_path}"

    async def aget_signed_url(self, key, **_):
        return f"https://storage.fake/get/{key}"

    async def aget_file_metadata(self, key):
        return {"key": key}


class FakeMps:
    """MPS keeps data per owner (organization id, or provider id in OSS mode)."""

    def __init__(self, world: ProbeWorld):
        self.keys: dict[str, list[dict]] = {}
        self.owner_tag: dict[str, str] = {}
        for t in (world.a, world.b):
            for owner in (str(t.org_id), str(t.user.provider_id)):
                self.owner_tag[owner] = t.tag
                self.keys[owner] = [
                    {
                        "id": 9000 + ord(t.tag),
                        "name": f"{t.token} skey",
                        "key_prefix": f"mps_{t.tag}",
                        "is_active": True,
                        "created_at": "2021-03-10T00:00:00Z",
                        "created_by": t.user.provider_id,
                        "key_id": t.service_key_id,
                    }
                ]
        self.tokens = {t.tag: t.token for t in (world.a, world.b)}

    @staticmethod
    def _owner(organization_id=None, created_by=None, **_):
        return str(organization_id if organization_id is not None else created_by)

    def _token(self, owner):
        return self.tokens[self.owner_tag[owner]]

    async def get_service_keys(self, include_archived=False, **kw):
        return list(self.keys.get(self._owner(**kw), []))

    async def create_service_key(self, name, expires_in_days=90, description="", **kw):
        return {
            "id": 9999,
            "name": name,
            "key_prefix": "mps_new",
            "is_active": True,
            "created_at": "2021-03-10T00:00:00Z",
            "service_key": f"sk-new-{self._owner(**kw)}",
        }

    async def archive_service_key(self, key_id, **kw):
        keys = self.keys.get(self._owner(**kw), [])
        return any(k["key_id"] == key_id for k in keys)

    async def get_voices(self, provider, **kw):
        return {
            "provider": provider,
            "voices": [{"voice_id": "v1", "name": f"{self._token(self._owner(**kw))}"}],
        }

    async def get_usage_by_created_by(self, created_by):
        self._token(str(created_by))
        return {"total_credits_used": 1.0, "remaining_credits": 2.0}

    async def get_usage_by_organization(self, organization_id):
        self._token(str(organization_id))
        return {"total_credits_used": 1.0, "remaining_credits": 2.0}

    async def get_billing_account_status(self, **kw):
        return {"billing_mode": "legacy"}

    async def transcribe_audio(self, **kw):
        return {"text": f"{self._token(self._owner(**kw))} said hi"}

    async def call_workflow_api(self, **kw):
        return {
            "name": f"{self._token(self._owner(**kw))} template wf",
            "workflow_definition": {"nodes": [], "edges": []},
        }

    async def __call__(self, *a, **kw):  # pragma: no cover - unexpected call
        raise AssertionError("unexpected MPS call")


class FakeEmbeddingService:
    """Real org-scoped chunk query, fixed query vector (no embedding API)."""

    def __init__(self, db):
        self.db = db

    async def search_similar_chunks(
        self, query, organization_id, limit=5, document_uuids=None
    ):
        return await self.db.search_similar_chunks(
            query_embedding=[0.1] * EMBED_DIM,
            organization_id=organization_id,
            limit=limit,
            document_uuids=document_uuids,
        )


class FakeProvider:
    PROVIDER_NAME = "livekit"
    WEBHOOK_ENDPOINT = "livekit"

    def validate_config(self):
        return True


@dataclass
class SignalingCalls:
    calls: list[tuple[int, int, int]] = field(default_factory=list)
