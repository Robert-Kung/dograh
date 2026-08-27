"use client";

import { ExternalLink } from "lucide-react";

import { MCPSection } from "@/components/MCPSection";
import { OrganizationPreferencesSection } from "@/components/OrganizationPreferencesSection";
import { TelemetrySection } from "@/components/TelemetrySection";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
// customer-center-platform fork（母 repo W2d task 3.3b）：
// 平台設定頁三個 section 的寫入**對兩個角色皆 deny**——
// `PUT /organizations/preferences`、`POST`／`DELETE /organizations/langfuse-credentials`
// （皆 `non-editor-ops`），連對應的 GET 也 deny（頁面本身會渲染錯誤態）。
// 這頁與 Models 頁同列在閘門「刻意保留」的可達頁清單裡，故不是死碼。
import { useCcpPageNotice } from "@/lib/ccp/notice-bar";

export default function SettingsPage() {
  useCcpPageNotice({
    supervisor: {
      title: '平台設定對您是唯讀的',
      message:
        '這頁的偏好設定、MCP 與遙測都由平台統一管理，兩種帳號在編輯器內都不能變更；'
        + '部分欄位甚至讀不到（畫面上會出現載入失敗）。需要調整時，請與您的專案窗口提出。',
    },
    implementer: {
      title: '平台設定的變更程序在部署層',
      message:
        '這頁的三個區塊在本部署都不經編輯器寫入（含讀取），閘門會拒絕。'
        + '請依 deploy/RUNBOOK.md 的對應程序在部署層變更。',
    },
  });
  return (
    <div className="flex justify-center py-12 px-4">
      <div className="w-full max-w-2xl space-y-6">
        <div>
          <h1 className="text-2xl font-bold">Platform Settings</h1>
          <p className="text-muted-foreground">
            Manage your platform configuration and integrations.
          </p>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Preferences</CardTitle>
            <CardDescription>
              Set organization-wide defaults such as the test phone number and
              timezone.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <OrganizationPreferencesSection />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>MCP Server</CardTitle>
            <CardDescription>
              Let AI agents access your Dograh workspace and documentation via
              the Model Context Protocol.{" "}
              <a
                href="https://docs.dograh.com/integrations/mcp"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 underline"
              >
                Learn more <ExternalLink className="h-3 w-3" />
              </a>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <MCPSection />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Telemetry</CardTitle>
            <CardDescription>
              Configure Langfuse tracing for your voice agent calls.{" "}
              <a
                href="https://docs.dograh.com/configurations/tracing"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 underline"
              >
                Learn more <ExternalLink className="h-3 w-3" />
              </a>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <TelemetrySection />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
