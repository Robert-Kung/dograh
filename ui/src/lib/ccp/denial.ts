/**
 * customer-center-platform fork：被拒絕的請求 → 可呈現的繁中說明
 * （母 repo W2d task 3.0／3.0b，U-18 2026-08-24 拍板）。
 *
 * 這個檔**不是**上游 dograh 的一部分（fork-only，W2d task 2.0c 的 rebase 紀律）。
 *
 * ## 文案的正本不在這裡
 *
 * 閘門（`services/editor-gateway/app/main.py` 的 `_notice()`）持有全部繁中文案，
 * 拒絕回應以 `ccp_notice` 信封帶回來。這裡**只負責取出與消毒**，
 * **MUST NOT** 自建「種類 → 文案」的對照表：那會讓同一段說明存在兩份
 * （母 repo 的 Python 與本 fork 的 TSX），跨 repo 各改各的，而這正是 U-18
 * 要消滅的形狀。這裡唯一自備的字串是「連種類都取不到」時的通用說明。
 *
 * ## 信任邊界（gate S-31）
 *
 * 閘門對**上游** JSON 錯誤是原樣轉發（只過 `mask_credentials`），所以一個 403
 * 也可能來自 dograh-api，body 無信封、`detail` 是英文甚至內部訊息
 * （`dograh#17` 的 `except Exception` 就把 ORM 內部訊息原樣放進 `detail`）。
 * 故：**只有信封裡的字才當成我方文案**；信封由閘門在轉發前自上游回應剝除
 * （`response_filter.strip_gateway_notice`），上游偽造不了。
 * 上游的 `detail` 只以「伺服器訊息」的身分、純文字、限長呈現，
 * 且通用說明 **MUST NOT** 宣稱拒絕的原因是角色——它可能根本不是。
 */

import { detailFromError } from '@/lib/apiError';

/** 與閘門 `app/response_filter.py` 的 `CCP_NOTICE_KEY` 同名。 */
export const CCP_NOTICE_KEY = 'ccp_notice';

export interface CcpDenialNotice {
    /** 閘門給的分類鍵。**不用來選文案**（那是對照表），只用於留痕與去重。 */
    kind: string;
    title: string;
    message: string;
    /** 文案是否由閘門產生。false ＝ 我方的通用說明。 */
    fromGateway: boolean;
    /** 上游原文（未受信任，僅在無信封時附上，已限長純文字化）。 */
    upstreamDetail?: string;
}

const MAX_TITLE = 80;
const MAX_MESSAGE = 400;
const MAX_DETAIL = 200;

/**
 * 純文字化：控制字元（含 CR/LF 與 ANSI 逸出）一律折成空白再收斂，最後限長。
 * 呈現端一律以文字節點渲染，**MUST NOT** `dangerouslySetInnerHTML`——
 * 這裡的消毒是為了不讓上游訊息在畫面上偽造版面，不是取代前者。
 */
function plainText(value: unknown, max: number): string {
    if (typeof value !== 'string') return '';
    const flattened = value.replace(/[\u0000-\u001F\u007F-\u009F]+/g, ' ');
    const collapsed = flattened.replace(/\s+/g, ' ').trim();
    return collapsed.length > max ? `${collapsed.slice(0, max - 1)}…` : collapsed;
}

/** 連種類都取不到時的說明。**不講角色**：這個分支下我們並不知道原因。 */
export const CCP_GENERIC_DENIAL = {
    title: '這個操作沒有送出',
    message:
        '伺服器拒絕了這次請求，而且沒有附上可辨識的原因分類，所以這裡說不出更具體的理由。'
        + '重試一次仍然失敗的話，請與負責建置的窗口聯繫。',
} as const;

/** 自回應 body 取出閘門的說明信封；不是閘門產生的一律回 null。 */
export function parseGatewayNotice(body: unknown): CcpDenialNotice | null {
    if (typeof body !== 'object' || body === null) return null;
    const raw = (body as Record<string, unknown>)[CCP_NOTICE_KEY];
    if (typeof raw !== 'object' || raw === null) return null;
    const envelope = raw as Record<string, unknown>;
    const title = plainText(envelope.title, MAX_TITLE);
    const message = plainText(envelope.message, MAX_MESSAGE);
    // 三個欄位缺任何一個就不是一個完整的信封——半個信封寧可落通用說明，
    // 也不要在畫面上出現一段沒有標題或沒有內容的說明。
    if (!title || !message || typeof envelope.kind !== 'string') return null;
    return { kind: envelope.kind, title, message, fromGateway: true };
}

/** 上游的 `detail`（未受信任）。只在沒有信封時才會被附上。 */
function upstreamDetail(body: unknown): string | undefined {
    if (typeof body !== 'object' || body === null) return undefined;
    const detail = (body as Record<string, unknown>).detail;
    const text = plainText(typeof detail === 'string' ? detail : '', MAX_DETAIL);
    return text || undefined;
}

/**
 * 把一個拒絕回應的 body 轉成可呈現的說明。**永遠有回傳值**——兜底的意義
 * 就在於「漏掉的入口也說得出一句話」。
 */
export function denialNoticeFromBody(body: unknown): CcpDenialNotice {
    const fromGateway = parseGatewayNotice(body);
    if (fromGateway) return fromGateway;
    return {
        kind: 'unknown',
        title: CCP_GENERIC_DENIAL.title,
        message: CCP_GENERIC_DENIAL.message,
        fromGateway: false,
        upstreamDetail: upstreamDetail(body),
    };
}

/**
 * 給**頁內**錯誤狀態用（W2d task 3.1 的 `setError` 那類）：SDK 的錯誤物件
 * 就是解析後的 body，所以同一支函式吃得下。
 *
 * 頁內狀態為什麼也要走這裡：`UploadWorkflowButton` 的 `catch` 無條件寫
 * 「Failed to upload workflow. Please check if the file is valid.」——主管上傳一份
 * **完全正確**的檔案會被告知「請檢查檔案是否有效」，那是**給了錯的原因**，
 * 比沒有原因更糟。兜底若只做 toast，頁內那句錯的話仍然留在畫面上。
 */
export function ccpDenialFromError(error: unknown): CcpDenialNotice | null {
    return parseGatewayNotice(error);
}

/** 頁內狀態的單行文字（標題與內容合併）。取不到閘門文案時回 null，由呼叫端決定。 */
export function ccpDenialText(error: unknown): string | null {
    const notice = ccpDenialFromError(error);
    return notice ? `${notice.title}：${notice.message}` : null;
}

/**
 * 頁內錯誤狀態的取用點：閘門有話說就用閘門的話，否則落回上游既有的
 * `detailFromError`。`fallback` 一律給繁中——上游那些
 * 「Failed to …」是英文，而本 change 新增的說明面 SHALL 是繁體中文。
 */
export function ccpErrorText(error: unknown, fallback: string): string {
    return ccpDenialText(error) ?? detailFromError(error, fallback);
}
