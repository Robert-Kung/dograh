import "./globals.css";

import type { Metadata } from "next";
import localFont from "next/font/local";
import { Suspense } from "react";

import ChatwootWidget from "@/components/ChatwootWidget";
import AppLayout from "@/components/layout/AppLayout";
import PostHogIdentify from "@/components/PostHogIdentify";
import { SentryErrorBoundary } from "@/components/SentryErrorBoundary";
import SpinLoader from "@/components/SpinLoader";
import { ThemeProvider } from "@/components/ThemeProvider";
import { Toaster } from "@/components/ui/sonner";
import { AppConfigProvider } from "@/context/AppConfigContext";
import { OnboardingProvider } from "@/context/OnboardingContext";
import { OrgConfigProvider } from "@/context/OrgConfigContext";
import { TelephonyConfigWarningsProvider } from "@/context/TelephonyConfigWarningsContext";
import { AuthProvider } from "@/lib/auth";
// customer-center-platform fork（母 repo W2d task 2.1）：app 層唯讀訊號。
// 必須在 AuthProvider 之內——它消費 useAuth()。
import { CcpAccessProvider } from "@/lib/ccp/access";


// 字型 self-host（母 repo W2d，2026-08-27）。
//
// 原本是 `next/font/google` 的 `Geist`／`Geist_Mono`——那會讓 **build 期**去 Google
// Fonts 取檔，於是「能不能建出 ui 映像」相依於一個外部服務。2026-08-27 的重灌演練
// 實際被它擋下一次：`[AggregateError] { code: 'ETIMEDOUT' }` → `Failed to fetch
// `Geist` from Google Fonts` → ui build 失敗；而首次部署沒有可降級的舊映像，
// `platform-up.sh` 只能拒絕啟動 ⇒ 一次網路抖動就能讓全新機器裝不起來。
//
// **執行期行為不變**：`next/font/google` 本來就是在 build 期抓檔並內聯進 bundle，
// 瀏覽器從不連 Google。改成 `local` 只是把「檔案從哪來」由網路換成版控。
//
// 檔案是 Google Fonts 對 `subsets: ["latin"]` 實際供應的那一份 woff2（variable，
// wght 100–900），與改動前 bundle 內的位元組相同。授權 SIL OFL 1.1，
// 全文與版權宣告見 `fonts/OFL.txt`（OFL 要求隨字型散布）。
const geistSans = localFont({
  src: "./fonts/Geist-latin.woff2",
  variable: "--font-geist-sans",
  weight: "100 900",
  display: "swap",
});

const geistMono = localFont({
  src: "./fonts/GeistMono-latin.woff2",
  variable: "--font-geist-mono",
  weight: "100 900",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Dograh",
  description: "Open Source Voice Assistant Workflow Builder",
};

export default function RootLayout({
  children
}: {
  children: React.ReactNode
}) {

  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        {/* Inline script to prevent flash of light theme - runs before React hydrates.
            Dark is the locked default: only an explicit stored 'light' opts out. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                try {
                  var theme = localStorage.getItem('theme');
                  if (theme === 'light') {
                    document.documentElement.classList.remove('dark');
                  } else {
                    document.documentElement.classList.add('dark');
                  }
                } catch (e) {
                  document.documentElement.classList.add('dark');
                }
              })();
            `,
          }}
        />
      </head>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}>
        <ThemeProvider attribute="class" defaultTheme="dark" enableSystem={false} disableTransitionOnChange>
          <SentryErrorBoundary>
            <AuthProvider>
              {/* fork patch 刻意不重排下方縮排：每次 rebase 的衝突面就只有這兩行。 */}
              <CcpAccessProvider>
              <AppConfigProvider>
                <Suspense fallback={<SpinLoader />}>
                  <OrgConfigProvider>
                    <TelephonyConfigWarningsProvider>
                      <OnboardingProvider>
                        <PostHogIdentify />
                        <AppLayout>
                          {children}
                        </AppLayout>
                        <Toaster />
                        <ChatwootWidget />
                      </OnboardingProvider>
                    </TelephonyConfigWarningsProvider>
                  </OrgConfigProvider>
                </Suspense>
              </AppConfigProvider>
              </CcpAccessProvider>
            </AuthProvider>
          </SentryErrorBoundary>
        </ThemeProvider>
      </body>
    </html>
  );
}
