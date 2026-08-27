"use client";

import { Check, Copy } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useAppConfig } from "@/context/AppConfigContext";
import { resolveBrowserBackendUrl } from "@/lib/apiClient";
import { useCcpAccess } from "@/lib/ccp/access";

const MCP_PATH = "/api/v1/mcp/";

export function MCPSection() {
  const { config } = useAppConfig();
  // customer-center-platform fork（母 repo W2d task 5.3c；**2026-08-25 文字審計後更正理由**）。
  //
  // **這一格是版面整潔，不是機密控制。** 原本的理由寫成「遮住基礎設施拓樸」，
  // 而那個前提是錯的，兩個值都不是：
  //   - `tunnelUrl` **早就到不了瀏覽器**——閘門在 `main.py` 的 `_config_version()`
  //     直接回 `None`（該處註解逐字：「對外隧道位址沒有進頁面的理由」），
  //     且 `/api/config/version` 由閘門攔在 UI 面全拒之前自產。`endpoints` 因此
  //     **對兩個角色都只有一個元素**。
  //   - `backendApiEndpoint` 等於 `EDITOR_PUBLIC_ORIGIN`（override 的
  //     `BACKEND_API_ENDPOINT`）——也就是**使用者網址列上那個位址**，不是私有 IP。
  //
  // 真正剩下的理由只有一條，而它成立：主管在本平台**用不到**這個端點
  //（MCP 接線是建置單位的工作，且 MCP authoring 本身是 CS-2「不一定開」），
  // 給他一個帶複製鈕的 `<code>` 只是噪音。
  //
  // **MUST NOT 被讀成保密**：`backendApiEndpoint` 仍在 `/api/config/version` 的
  // 回應裡、任何角色都取得到（閘門那支沒有角色分支），也還在 SDK 的 baseUrl 與
  // devtools 的 Network 面板上。角色訊號本來就住在瀏覽器裡、可被改寫。
  // 訊號不可得時走不顯示那側（保守的呈現預設）。
  const { role } = useCcpAccess();
  const showEndpoints = role === "implementer";
  // Backend URL: the address the deployment runs on (a private IP when the backend
  // sits on one). Tunnel URL, when present: the publicly reachable Cloudflare tunnel
  // URL externally-hosted assistants should use to reach an otherwise-private host.
  const backendUrl = resolveBrowserBackendUrl(config?.backendApiEndpoint);
  const tunnelUrl = config?.tunnelUrl ?? null;

  const endpoints = [
    ...(tunnelUrl
      ? [
          {
            key: "tunnel",
            label: "Public URL (Cloudflare tunnel)",
            url: `${tunnelUrl}${MCP_PATH}`,
          },
        ]
      : []),
    { key: "backend", label: "Backend URL", url: `${backendUrl}${MCP_PATH}` },
  ];

  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const handleCopy = async (value: string, key: string) => {
    await navigator.clipboard.writeText(value);
    setCopiedKey(key);
    setTimeout(
      () => setCopiedKey((current) => (current === key ? null : current)),
      2000,
    );
  };

  return (
    <div className="grid gap-6">
      <div className="grid gap-2">
        <Label>MCP Endpoint</Label>
        <p className="text-xs text-muted-foreground">
          Connect an MCP-compatible AI assistant to this URL over Streamable
          HTTP. Requires an API key in the X-API-Key header.{" "}
          {/* customer-center-platform fork（母 repo W2d task 3.6b）：
              上游的「Get your API key」連到 `/api-keys`，該頁在閘門的 UI 拒絕清單內
              ⇒ 開新分頁得 403。與 `DocumentSelector` 那顆完全同型，依 task 3.2b
              的「入口本身通往不可達頁面」例外移除。 */}
          <span className="text-muted-foreground">
            金鑰由建置單位配發，請與您的專案窗口索取。
          </span>
        </p>
        {!showEndpoints && (
          <p className="text-xs text-muted-foreground">
            端點位址（部署主機／對外通道）不在本畫面呈現，請與您的專案窗口索取。
          </p>
        )}
        <div className="grid gap-3">
          {showEndpoints && endpoints.map(({ key, label, url }) => (
            <div key={key} className="grid gap-1">
              {endpoints.length > 1 && (
                <span className="text-xs font-medium text-muted-foreground">
                  {label}
                </span>
              )}
              <div className="flex items-center gap-2">
                <code className="text-xs break-all bg-muted px-2 py-1 rounded flex-1">
                  {url}
                </code>
                <Button
                  variant="outline"
                  size="icon"
                  className="shrink-0"
                  onClick={() => handleCopy(url, key)}
                >
                  {copiedKey === key ? (
                    <Check className="h-4 w-4" />
                  ) : (
                    <Copy className="h-4 w-4" />
                  )}
                </Button>
              </div>
            </div>
          ))}
        </div>
        {showEndpoints && tunnelUrl && (
          <p className="text-xs text-muted-foreground">
            Use the public URL from externally-hosted assistants; the backend URL
            works from the deployment&apos;s own network.
          </p>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        For step-by-step setup with Claude Code, Claude Desktop, Cursor, and
        other clients, see the{" "}
        <Link
          href="https://docs.dograh.com/integrations/mcp"
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary underline hover:no-underline"
        >
          MCP integration guide
        </Link>
        .
      </p>
    </div>
  );
}
