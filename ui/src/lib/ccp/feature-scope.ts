/**
 * customer-center-platform fork：前端持有的**平台政策副本**（母 repo W2d task 4.1c）。
 *
 * 這個檔**不是**上游 dograh 的一部分（fork-only，W2d task 2.0c）。
 *
 * ## 為什麼要有一份副本（U-22，2026-08-24 拍板）
 *
 * 正本是 `deploy/feature-scope.json`，**在容器外、瀏覽器拿不到**——override 的
 * `ui:` 區段只有 `ports` 與 `environment`，沒有掛載它，也沒有端點把它投影出來。
 *
 * 取「② 硬編 ＋ preflight 比對」而不是「① 閘門新增唯讀端點投影」：
 * 後者會讓客戶瀏覽器多一條可打的路由，需同批進 `route-classification.yml`
 * 與 UI 平面的相容判定，且會使 proposal「唯一觸及母 repo 閘門的改動是
 * `_deny()` 帶 `kind`」變成假。而 `feature-scope.json` 的既有讀者本來就是
 * preflight §7 與 `dograh-bootstrap.py`——② 把比對放在**已經讀它的那一層**，
 * 失效時在**部署期大聲失敗**而非執行期靜默漂移。
 *
 * ## 失效機制（沒有這一條，本檔就只是一份會過期的註解）
 *
 * `deploy/preflight.sh` 有一條比對：本檔的三組常數 vs `deploy/feature-scope.json`，
 * **不一致即 fail**。它讀的是**版控原始檔**，不是執行中的 bundle——
 * 沒有 build 時序依賴，失敗改原始碼即可復原，故放 preflight 正確
 * （與 2.0d「載體 MUST NOT 是 `preflight.sh`」不衝突，準則見 task 4.1c）。
 *
 * **這條比對的是 submodule 工作副本**，未 bump pointer 時仍會綠（gate L-G）；
 * 那一軸由 `2.0b`／`AC8b`／`7.4` 的從零 `git clone --recursive` 承擔。
 *
 * ## 前端要持有的三組政策知識
 *
 * ① 啟用的工具類型集合 —— `CCP_ALLOWED_TOOL_TYPES`／`CCP_BLOCKED_TOOL_TYPES`
 * ② `transfer_call` 的**欄位層**額外規則 —— `CCP_TOOL_TYPE_REQUIRED_KEYS`。
 *    它**不在** `blocked_tool_types` 裡：`transfer_call` 是允許的類型，
 *    deny 來自 `required_keys` ＋ 遮罩哨兵。**只讀①的話會漏掉它**，
 *    而「UI 說可以選、正本已封鎖」正是 AC5 要消滅的「選了才失敗」。
 * ③ coverage-map 的 deny 清單 —— 不在本檔，落在各頁的 `ccpDisabledProps()`
 *    與 `useCcpPageNotice()`（那是逐頁處置，不是一份可比對的清單）。
 *
 * ## 這份副本**不是**執行點
 *
 * 授權的唯一執行點是閘門（`Allowlist.decide()` ＋ admission）。本檔只決定
 * **畫面上長什麼樣**。呈現面失效 MUST NOT 導致授權失效——反過來也一樣：
 * 本檔漏了一項，後果是使用者選得到、送出去被擋，不是擋不住。
 */

import type { ToolCategory } from '@/app/tools/config';

/** `deploy/feature-scope.json` 的 `allowed_tool_types`。 */
export const CCP_ALLOWED_TOOL_TYPES: readonly ToolCategory[] = [
    'transfer_call',
    'end_call',
];

/** `deploy/feature-scope.json` 的 `blocked_tool_types`。 */
export const CCP_BLOCKED_TOOL_TYPES: readonly ToolCategory[] = [
    'calculator',
    'http_api',
    'mcp',
    'native',
    'integration',
];

/**
 * `field_rules.required_keys` ＋ `field_rules.required_keys_when_tool_type`。
 *
 * 形狀刻意做成 map 而不是兩個平行常數：正本是「這些鍵在**這個**工具類型上必填」，
 * 拆成兩個常數的話，日後正本多一個類型時這裡會靜默只覆蓋第一個。
 *
 * ## W3a：改錨，**不是**清空
 *
 * 設定分層把 `queueHealthUrl`／`queueHealthToken` 移出工具設定（改由部署層 env
 * 供給），正本的必要鍵因此**改錨**在留下來的兩條失敗路徑話術上。
 *
 * 天真的「同批清空」在這裡有一個具體的壞結局，而它正是本檔 `:84-88` 逐字警告的
 * 那一格：清空 → `ccpToolTypeAdmission` 回 `selectable: true` → `transfer_call`
 * 在建立對話框裡變成可選 → 預設 definition 帶 `destination` → 命中正本新增的
 * `forbidden_keys` → **送出必然 403**。使用者看到的是「UI 說可以建，建了就失敗」。
 *
 * 改錨之後 `selectable: false` 與 `CCP_DEFAULT_TOOL_CATEGORY` 都**自動維持不變**
 * ——兩者都是集合運算而非硬編值（見下方兩處），所以這一格不需要額外的補償邏輯。
 */
export const CCP_TOOL_TYPE_REQUIRED_KEYS: Readonly<Partial<Record<ToolCategory, readonly string[]>>> = {
    transfer_call: ['transferFailedMessage', 'transferUnavailableMessage'],
};

export interface CcpToolTypeAdmission {
    /** 這個類型能不能在「建立工具」對話框裡選。 */
    selectable: boolean;
    /** 不能選（或選了也會失敗）的原因，繁中，直接上畫面。空字串＝沒有話要說。 */
    reason: string;
}

/**
 * 一個工具類型在本部署的建立面待遇。
 *
 * **三種結果，不是兩種**：
 *   - 封鎖類型 → 不可選，原因是「本部署未開放這個類型」。
 *   - 允許但**預設 definition 湊不齊必要鍵**（今天只有 `transfer_call`）
 *     → 也不可選，但原因完全不同：能力有、只是不從這裡建。
 *     N3 要求這一格要**標明**，因為它是最容易被讀成 bug 的一格
 *     （UI 讓你選、送出去卻必然 403）。
 *   - 其餘允許類型 → 可選。
 *
 * **不寫死數字**（gate L-2／T16）：`TOOL_CATEGORIES` 現為 7 筆、其中 `native`
 * 與 `integration` 上游已帶 `disabled`，任何「五種／三種」式的敘述都對不上，
 * 且上游一改就錯。判準是集合運算，不是計數。
 */
export function ccpToolTypeAdmission(category: ToolCategory): CcpToolTypeAdmission {
    if (CCP_BLOCKED_TOOL_TYPES.includes(category)) {
        return {
            selectable: false,
            reason: '本部署未開放這個工具類型：交付範圍是話術與轉接設定的維護。',
        };
    }
    const requiredKeys = CCP_TOOL_TYPE_REQUIRED_KEYS[category];
    if (requiredKeys && requiredKeys.length > 0) {
        return {
            selectable: false,
            reason:
                `${category} 的必要欄位（${requiredKeys.join('、')}）新建時湊不齊，`
                + '送出會被內容檢查擋下。'
                + '轉接工具已由建置單位配置好，需要調整請與您的專案窗口提出。',
        };
    }
    return { selectable: true, reason: '' };
}

/**
 * 建立對話框的預設類型（task 4.1b）。
 *
 * 上游預設是 `http_api`，而它在 `blocked_tool_types` 內——只做「不可選」會留下
 * 一個**當前值即為 disabled 項**的 Select（選單打開全灰、關起來卻顯示一個
 * 選不回去的值）。取第一個 `selectable` 的類型，**不硬編**：正本改了就跟著改，
 * 而 preflight 的比對保證正本與本檔同步。
 */
export const CCP_DEFAULT_TOOL_CATEGORY: ToolCategory =
    CCP_ALLOWED_TOOL_TYPES.find((c) => ccpToolTypeAdmission(c).selectable) ?? 'end_call';
