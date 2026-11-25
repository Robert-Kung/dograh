"""ROUTE_SCOPE — every dograh route in one of three classes (mt-s1-foundation 8.3).

Checked by ``test_route_inventory.py`` (rules in ``route_inventory.py``). A new
route fails CI until it is classified here. Skeleton: the parent repo's
``deploy/workflow-editor-gateway/route-inventory/route-classification.yml``.

- (a) ``ORG_SCOPED``: route -> isolation-test node ids that hit it.
- (b) ``EXEMPT``: route -> reason. Unreachable-in-delivery reasons rely on
  nginx having no direct dograh location and the editor gateway denying the
  route (route-classification.yml).
- (c) ``PROBED``: upstream org-filtered routes proven by the cross-org probe
  (8.3a). Shrink-only: limited to ``route_probe_snapshot.txt``.
"""

ORG_SCOPED: dict[str, tuple[str, ...]] = {
    "POST /api/v1/internal/call-records/query": (
        "tests/tenant_isolation/test_ccp_tenant_scope.py::test_list_only_shows_own_organization",
    ),
    "GET /api/v1/internal/call-records/{run_id}": (
        "tests/tenant_isolation/test_ccp_tenant_scope.py::test_other_org_detail_is_not_found",
    ),
    "GET /api/v1/internal/call-records/{run_id}/audio": (
        "tests/tenant_isolation/test_ccp_tenant_scope.py::test_other_org_audio_is_not_found",
    ),
    "GET /api/v1/internal/usage-report": (
        "tests/tenant_isolation/test_ccp_tenant_scope.py::test_usage_report_counts_own_organization",
    ),
    "GET /api/v1/s3/file-metadata": (
        "tests/tenant_isolation/test_s3_file_metadata.py::test_other_org_run_metadata_is_denied",
        "tests/tenant_isolation/test_s3_file_metadata.py::test_voicemail_metadata_is_superuser_only",
    ),
}

_UNREACHABLE = "；交付態不可達（nginx 無 dograh 直連，編輯器閘門 deny）"
_CARRIER_WEBHOOK = (
    "承載商 webhook，無使用者身分（以承載商簽章或回呼參數識別通話；本平台承載商僅 LiveKit）"
    + _UNREACHABLE
)

EXEMPT: dict[str, str] = {
    "GET /api/v1/superuser/workflow-runs": "is_superuser 專用（get_superuser）；租戶成員與編輯器服務身分非 superuser 由 test_tenant_attribution_lint.py::test_tenant_members_are_not_superusers 斷言",
    "POST /api/v1/superuser/impersonate": "is_superuser 專用（get_superuser）；租戶成員與編輯器服務身分非 superuser 由 test_tenant_attribution_lint.py::test_tenant_members_are_not_superusers 斷言",
    "GET /api/v1/openapi.json": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "GET /docs": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "GET /docs/oauth2-redirect": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "GET /redoc": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "HEAD /api/v1/openapi.json": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "HEAD /docs": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "HEAD /docs/oauth2-redirect": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "HEAD /redoc": "公開 API 文件，無租戶資料" + _UNREACHABLE,
    "GET /api/v1/health": "公開健康檢查，無租戶資料" + _UNREACHABLE,
    "GET /api/v1/health/active-calls": "部署層密鑰（X-Dograh-Devops-Secret）：排空訊號，回全平台進行中通話數，不含租戶識別碼",
    "POST /api/v1/auth/signup": "部署層密鑰（signup 例外，含空 DB 建第一號租戶）；匿名註冊被拒由 test_signup_gate.py 涵蓋",
    "POST /api/v1/auth/login": "登入面：取得身分之前的端點，回應只含登入者自身"
    + _UNREACHABLE,
    "POST /api/v1/livekit/inbound/{secret}": "部署層密鑰（path secret＋LiveKit webhook 簽章）：來電分派，組織取自號碼列（test_two_tenants.py 涵蓋）",
    "WS /api/v1/telephony/ws/{workflow_id}/{user_id}/{workflow_run_id}": "承載商 websocket 無身分依賴、run id 自增可猜，搶先連線可使他租戶 run 被設 RUNNING、真來電被拒——殘留風險 R-MT-11"
    + _UNREACHABLE,
    "WS /api/v1/agent-stream/{workflow_uuid}": "無使用者身分的 websocket（承載商／公開嵌入）"
    + _UNREACHABLE,
    "WS /api/v1/telephony/ws/ari": "無使用者身分的 websocket（承載商／公開嵌入）"
    + _UNREACHABLE,
    "WS /api/v1/ws/public/signaling/{session_token}": "無使用者身分的 websocket（承載商／公開嵌入）"
    + _UNREACHABLE,
    "GET /api/v1/public/download/workflow/{token}/{artifact_type}": "公開嵌入／下載面，以 path／body 的 token 授權，無使用者身分依賴"
    + _UNREACHABLE,
    "GET /api/v1/public/embed/config/{token}": "公開嵌入／下載面，以 path／body 的 token 授權，無使用者身分依賴"
    + _UNREACHABLE,
    "GET /api/v1/public/embed/turn-credentials/{session_token}": "公開嵌入／下載面，以 path／body 的 token 授權，無使用者身分依賴"
    + _UNREACHABLE,
    "OPTIONS /api/v1/public/embed/config/{token}": "公開嵌入／下載面，以 path／body 的 token 授權，無使用者身分依賴"
    + _UNREACHABLE,
    "OPTIONS /api/v1/public/embed/init": "公開嵌入／下載面，以 path／body 的 token 授權，無使用者身分依賴"
    + _UNREACHABLE,
    "OPTIONS /api/v1/public/embed/turn-credentials/{session_token}": "公開嵌入／下載面，以 path／body 的 token 授權，無使用者身分依賴"
    + _UNREACHABLE,
    "POST /api/v1/public/embed/init": "公開嵌入／下載面，以 path／body 的 token 授權，無使用者身分依賴"
    + _UNREACHABLE,
    "POST /api/v1/public/agent/test/workflow/{workflow_uuid}": "X-API-Key 於 handler 內驗證（無 FastAPI 身分依賴，盤點無法判定、探針不適用）"
    + _UNREACHABLE,
    "POST /api/v1/public/agent/test/{uuid}": "X-API-Key 於 handler 內驗證（無 FastAPI 身分依賴，盤點無法判定、探針不適用）"
    + _UNREACHABLE,
    "POST /api/v1/public/agent/workflow/{workflow_uuid}": "X-API-Key 於 handler 內驗證（無 FastAPI 身分依賴，盤點無法判定、探針不適用）"
    + _UNREACHABLE,
    "POST /api/v1/public/agent/{uuid}": "X-API-Key 於 handler 內驗證（無 FastAPI 身分依賴，盤點無法判定、探針不適用）"
    + _UNREACHABLE,
    "MOUNT /api/v1/mcp": "MCP 子應用，X-API-Key 於子應用內驗證（無 FastAPI 依賴樹，盤點無法判定、探針不適用）"
    + _UNREACHABLE,
    "GET /api/v1/user/configurations/defaults": "全域靜態資料（範本／預設值），無身分依賴、無租戶資料",
    "GET /api/v1/workflow/templates": "全域靜態資料（範本／預設值），無身分依賴、無租戶資料",
    "POST /api/v1/telephony/inbound/run": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/inbound/fallback": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/inbound/{workflow_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/transfer-result/{transfer_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/cloudonix/status-callback/{workflow_run_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/cloudonix/cdr": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/plivo-xml": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/plivo/hangup-callback/{workflow_run_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/plivo/ring-callback/{workflow_run_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/telnyx/events/{workflow_run_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/telnyx/transfer-result/{transfer_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/twiml": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/twilio/status-callback/{workflow_run_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/vobiz-xml": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/vobiz/hangup-callback/{workflow_run_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/vobiz/ring-callback/{workflow_run_id}": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/vobiz/hangup-callback/workflow/{workflow_id}": _CARRIER_WEBHOOK,
    "GET /api/v1/telephony/ncco": _CARRIER_WEBHOOK,
    "POST /api/v1/telephony/vonage/events/{workflow_run_id}": _CARRIER_WEBHOOK,
}

PROBED: tuple[str, ...] = (
    "POST /api/v1/telephony/initiate-call",
    "POST /api/v1/livekit/did-mappings",
    "POST /api/v1/workflow/{workflow_id}/validate",
    "POST /api/v1/workflow/create/definition",
    "POST /api/v1/workflow/create/template",
    "GET /api/v1/workflow/count",
    "GET /api/v1/workflow/fetch",
    "GET /api/v1/workflow/fetch/{workflow_id}",
    "GET /api/v1/workflow/{workflow_id}/versions",
    "POST /api/v1/workflow/{workflow_id}/publish",
    "POST /api/v1/workflow/{workflow_id}/create-draft",
    "GET /api/v1/workflow/summary",
    "PUT /api/v1/workflow/{workflow_id}/status",
    "PUT /api/v1/workflow/{workflow_id}/folder",
    "PUT /api/v1/workflow/{workflow_id}",
    "POST /api/v1/workflow/{workflow_id}/duplicate",
    "POST /api/v1/workflow/{workflow_id}/runs",
    "GET /api/v1/workflow/{workflow_id}/runs/{run_id}",
    "GET /api/v1/workflow/{workflow_id}/runs",
    "GET /api/v1/workflow/{workflow_id}/report",
    "POST /api/v1/workflow/templates/duplicate",
    "POST /api/v1/workflow/ambient-noise/upload-url",
    "POST /api/v1/workflow/{workflow_id}/text-chat/sessions",
    "GET /api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}",
    "POST /api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}/messages",
    "POST /api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}/rewind",
    "GET /api/v1/user/auth/user",
    "GET /api/v1/user/configurations/user",
    "PUT /api/v1/user/configurations/user",
    "GET /api/v1/user/onboarding-state",
    "PUT /api/v1/user/onboarding-state",
    "GET /api/v1/user/configurations/user/validate",
    "GET /api/v1/user/api-keys",
    "POST /api/v1/user/api-keys",
    "DELETE /api/v1/user/api-keys/{api_key_id}",
    "PUT /api/v1/user/api-keys/{api_key_id}/reactivate",
    "GET /api/v1/user/configurations/voices/{provider}",
    "POST /api/v1/campaign/create",
    "GET /api/v1/campaign/",
    "GET /api/v1/campaign/{campaign_id}",
    "POST /api/v1/campaign/{campaign_id}/start",
    "POST /api/v1/campaign/{campaign_id}/pause",
    "PATCH /api/v1/campaign/{campaign_id}",
    "GET /api/v1/campaign/{campaign_id}/runs",
    "POST /api/v1/campaign/{campaign_id}/redial",
    "POST /api/v1/campaign/{campaign_id}/resume",
    "GET /api/v1/campaign/{campaign_id}/progress",
    "GET /api/v1/campaign/{campaign_id}/source-download-url",
    "GET /api/v1/campaign/{campaign_id}/report",
    "GET /api/v1/credentials/",
    "POST /api/v1/credentials/",
    "GET /api/v1/credentials/{credential_uuid}",
    "PUT /api/v1/credentials/{credential_uuid}",
    "DELETE /api/v1/credentials/{credential_uuid}",
    "GET /api/v1/tools/",
    "POST /api/v1/tools/",
    "GET /api/v1/tools/{tool_uuid}",
    "POST /api/v1/tools/{tool_uuid}/mcp/refresh",
    "PUT /api/v1/tools/{tool_uuid}",
    "DELETE /api/v1/tools/{tool_uuid}",
    "POST /api/v1/tools/{tool_uuid}/unarchive",
    "GET /api/v1/organizations/context",
    "GET /api/v1/organizations/telephony-providers/metadata",
    "GET /api/v1/organizations/telephony-config-warnings",
    "GET /api/v1/organizations/model-configurations/v2/defaults",
    "GET /api/v1/organizations/model-configurations/v2",
    "PUT /api/v1/organizations/model-configurations/v2",
    "GET /api/v1/organizations/model-configurations/v2/migration-preview",
    "POST /api/v1/organizations/model-configurations/v2/migrate",
    "GET /api/v1/organizations/preferences",
    "PUT /api/v1/organizations/preferences",
    "GET /api/v1/organizations/model-configurations/preferences",
    "PUT /api/v1/organizations/model-configurations/preferences",
    "GET /api/v1/organizations/telephony-configs",
    "POST /api/v1/organizations/telephony-configs",
    "GET /api/v1/organizations/telephony-configs/{config_id}",
    "PUT /api/v1/organizations/telephony-configs/{config_id}",
    "POST /api/v1/organizations/telephony-configs/{config_id}/set-default-outbound",
    "DELETE /api/v1/organizations/telephony-configs/{config_id}",
    "GET /api/v1/organizations/telephony-configs/{config_id}/phone-numbers",
    "POST /api/v1/organizations/telephony-configs/{config_id}/phone-numbers",
    "GET /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}",
    "PUT /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}",
    "POST /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}/set-default-caller",
    "DELETE /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}",
    "GET /api/v1/organizations/telephony-config",
    "POST /api/v1/organizations/telephony-config",
    "GET /api/v1/organizations/langfuse-credentials",
    "POST /api/v1/organizations/langfuse-credentials",
    "DELETE /api/v1/organizations/langfuse-credentials",
    "GET /api/v1/organizations/campaign-defaults",
    "GET /api/v1/s3/signed-url",
    "POST /api/v1/s3/presigned-upload-url",
    "GET /api/v1/user/service-keys",
    "POST /api/v1/user/service-keys",
    "DELETE /api/v1/user/service-keys/{service_key_id}",
    "PUT /api/v1/user/service-keys/{service_key_id}/reactivate",
    "GET /api/v1/organizations/usage/current-period",
    "GET /api/v1/organizations/usage/mps-credits",
    "GET /api/v1/organizations/billing/credits",
    "POST /api/v1/organizations/usage/mps-credits/purchase-url",
    "GET /api/v1/organizations/usage/runs",
    "GET /api/v1/organizations/usage/runs/report",
    "GET /api/v1/organizations/usage/daily-breakdown",
    "GET /api/v1/organizations/reports/daily",
    "GET /api/v1/organizations/reports/workflows",
    "GET /api/v1/organizations/reports/daily/runs",
    "WS /api/v1/ws/signaling/{workflow_id}/{workflow_run_id}",
    "GET /api/v1/turn/credentials",
    "POST /api/v1/workflow/{workflow_id}/embed-token",
    "GET /api/v1/workflow/{workflow_id}/embed-token",
    "DELETE /api/v1/workflow/{workflow_id}/embed-token",
    "POST /api/v1/knowledge-base/upload-url",
    "POST /api/v1/knowledge-base/process-document",
    "GET /api/v1/knowledge-base/documents",
    "GET /api/v1/knowledge-base/documents/{document_uuid}",
    "DELETE /api/v1/knowledge-base/documents/{document_uuid}",
    "POST /api/v1/knowledge-base/search",
    "POST /api/v1/workflow-recordings/upload-url",
    "POST /api/v1/workflow-recordings/",
    "GET /api/v1/workflow-recordings/",
    "DELETE /api/v1/workflow-recordings/{recording_id}",
    "PATCH /api/v1/workflow-recordings/{id}",
    "POST /api/v1/workflow-recordings/transcribe",
    "GET /api/v1/folder/",
    "POST /api/v1/folder/",
    "PUT /api/v1/folder/{folder_id}",
    "DELETE /api/v1/folder/{folder_id}",
    "GET /api/v1/auth/me",
    "GET /api/v1/node-types",
    "GET /api/v1/node-types/{name}",
)
