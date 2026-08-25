'use client';

/**
 * customer-center-platform fork：app 層的唯讀訊號（母 repo W2d D-D1／2.1／2.1b／2.4）。
 *
 * 這個檔**不是**上游 dograh 的一部分。集中放在這裡是為了把 rebase 衝突面壓到
 * 最小（W2d task 2.0c）：上游檔案內只留「呼叫一個 hook」這種最小接點，判定邏輯、
 * fail-safe 規則與文案分支全部在 fork-only 的檔案裡。
 *
 * ## 訊號來源
 *
 * `/api/auth/oss` —— 在本平台**不是** dograh-ui 自己那支 route，而是由編輯器閘門
 * 自產（`services/editor-gateway/app/main.py` 的 `_oss_auth_identity`，W2c task 4.5）。
 * 回應的 `user` 帶 `role`，`token` 一律空字串。既有的 `LocalProviderWrapper` 已經
 * 會去取它並把 `user` 放進 `AuthContext`，故這裡直接消費 `useAuth()`，不另開一支 fetch。
 *
 * ## 這**不是**授權（W2d task 2.4）
 *
 * `role` 走的是瀏覽器可見、可改寫的路徑（cookie → 閘門回應 → JS）。任何人都能在
 * devtools 裡把它改成 `implementer` 而讓按鈕變回可按。**授權在伺服端**——閘門的
 * `decide()` 依 session（不是這個欄位）判定，該擋的照擋、回 403。這裡畫的是介面，
 * 目的是不要讓主管對著一個按了會報英文錯的按鈕操作，MUST NOT 被當成安全邊界。
 *
 * ## 訊號不可得時一律唯讀（W2d task 2.1b／D-D9）
 *
 * 四種失效——未認證、fetch 失敗、`role` 欄缺失、未知角色值——**一律**判唯讀。
 * 沿用既有慣例 `useWorkflowOptional()?.readOnly ?? false`（＝可寫）會在載入中與
 * 取訊號失敗時開出一個可寫窗口，而「閘門 deny `/api/auth/oss` 使 SPA 端 auth
 * 永不 ready」在本平台是實際發生過的路徑，不是假想。
 *
 * 但 fail-safe 單獨存在有反向危害：一個**實施方** session 在閘門設定錯誤下會看到
 * 「運作正常的唯讀介面 ＋ 滿頁『請找實施方』」，而沒有任何一處說得出「訊號取不到」。
 * 故狀態**區分** `readonly-role`（已確認是唯讀角色）與 `signal-unavailable`
 * （訊號不可得），兩者 MUST NOT 共用同一段說明文案。
 */

import React, { createContext, useContext, useEffect, useMemo } from 'react';

import { useAuth } from '@/lib/auth';

import {
    CCP_ACCESS_FALLBACK as FALLBACK,
    resolveAccess,
} from './access-rules';
import type { CcpAccess } from './access-rules';
import { installCcpDenialFallback } from './denial-fallback';
import { CCP_FORK_MARKER } from './fork-marker';

/**
 * **判定邏輯住在 `./access-rules`**（W2d task 7.1）：那個檔不 import React，
 * 因此四條 fail-safe 分支可以用上游既有的 `.mts` 慣例直接測
 * （`scripts/ccp-access-rules.test.mts`，零依賴）。本檔原樣 re-export，
 * 呼叫端的 import 不必改。
 */
export {
    CCP_KNOWN_ROLES,
    CCP_ACCESS_FALLBACK,
    localRole,
    resolveAccess,
    WRITABLE_ROLES,
} from './access-rules';
export type { CcpAccess, CcpAccessState, CcpRole } from './access-rules';

const CcpAccessContext = createContext<CcpAccess>(FALLBACK);

export function CcpAccessProvider({ children }: { children: React.ReactNode }) {
    const { user, isAuthenticated, loading } = useAuth();

    // 全域 403 兜底（W2d task 3.0）。掛在這裡是因為這是 fork 在 root layout
    // 上唯一的接點——多一個 provider 就多一段 rebase 衝突面。安裝本身冪等，
    // strict mode 跑兩次不會疊出兩張 toast。
    useEffect(() => {
        installCcpDenialFallback();
    }, []);

    const value = useMemo(
        () => resolveAccess({ loading, isAuthenticated, user }),
        [loading, isAuthenticated, user],
    );

    return (
        <CcpAccessContext.Provider value={value}>
            {/* fork 標記的渲染點（W2d task 2.0d）。這個節點存在的唯一目的是讓
                `CCP_FORK_MARKER` 有一個 tree-shaking 拿不掉的使用點，且讓字面
                進到 client bundle 供 `check-runtime-consistency.sh` grep。 */}
            <span hidden data-ccp-fork={CCP_FORK_MARKER} />
            {children}
        </CcpAccessContext.Provider>
    );
}

/** 完整狀態（要分辨 `readonly-role` 與 `signal-unavailable` 的說明條用）。 */
export function useCcpAccess(): CcpAccess {
    return useContext(CcpAccessContext);
}

/** 只要「能不能寫」的地方用這支。context 缺失時回 true（唯讀）。 */
export function useCcpReadOnly(): boolean {
    return useContext(CcpAccessContext).readOnly;
}
