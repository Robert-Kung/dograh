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
  // customer-center-platform fork（母 repo W2d task 5.3c，殘留 R-X 重評）：
  // 這兩個位址是**基礎設施拓樸**——`backendApiEndpoint` 是部署主機（常是私有 IP）、
  // `tunnelUrl` 是對外的 Cloudflare tunnel URL，上游把它們渲染成**可複製的 `<code>`**。
  // W2c 讓主管首次取得這頁的讀取面，W2d 把它從「API 回應裡」搬到「主管天天看的畫面上」。
  // 主管在本平台**沒有任何用途需要這兩個位址**（MCP 接線是建置單位的工作），故對
  // 非實施方只給摘要。
  // **這不是授權執行點**：角色訊號住在瀏覽器裡、可被改寫，真正的邊界是閘門的
  // `response_filter`（而 `base_url` 形狀的鍵不在它的憑證鍵集合內——R-X 未關閉）。
  // 訊號不可得時走遮罩側（fail-closed 的方向是少顯示）。
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
