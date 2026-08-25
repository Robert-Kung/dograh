/**
 * customer-center-platform fork：全域 403 兜底（母 repo W2d task 3.0，gate T2）。
 *
 * 這個檔**不是**上游 dograh 的一部分（fork-only，W2d task 2.0c）。
 *
 * ## 為什麼要兜底
 *
 * 逐頁處置是**列舉式**的，而本 change 的覆蓋面列舉已連續三輪漏項。兜底不會讓
 * 漏項消失，但把漏項的後果從「一個沒有原因的英文錯誤」降為「有原因但不夠具體的
 * 說明」。兩者並存，**不得以其一取代其二**。
 *
 * ## 為什麼掛在 `window.fetch` 而不是 SDK 的 interceptor
 *
 * SDK interceptor 只涵蓋 `src/client/sdk.gen.ts` 的呼叫；raw `fetch()` 呼叫點
 * （`components/flow/mcpRefresh.ts`、`useWebSocketRTC.tsx` …）落在它外面，而
 * 「逐一補上」就是同一個列舉式覆蓋面——那正是本 change 被退回的形狀。
 * SDK 自己的請求最終也經 `globalThis.fetch`（`client.gen.ts:45`），
 * 所以這一層是**唯一的觸發點**，也就不需要跨兩層去重。
 *
 * 包裝是**被動觀測**：原樣回傳上游的 `Response`，只在 403 時複製一份讀 body。
 * 不改狀態碼、不改 body、不吞錯誤——呼叫端既有的錯誤處理完全不受影響。
 *
 * ## 兜底 MUST NOT 靜默（gate S-12／S-18）
 *
 * 每次觸發至少留一筆可見痕跡：畫面上的 toast、console 的一行、以及
 * `window.__ccpDenials` 的一筆紀錄。第三者是給 §6 巡檢用的——判準欄的
 * 「本頁零非預期 403」需要一個機器讀得到的面，靠人盯畫面數不出來。
 */

import { toast } from 'sonner';

import { type CcpDenialNotice, denialNoticeFromBody } from './denial';

export interface CcpDenialRecord {
    kind: string;
    method: string;
    /** 只留 path，query 可能帶識別資訊，而這份紀錄會被巡檢腳本讀出來。 */
    path: string;
    fromGateway: boolean;
    at: number;
}

const RECORD_LIMIT = 50;
const denials: CcpDenialRecord[] = [];

declare global {
    interface Window {
        __ccpDenials?: CcpDenialRecord[];
    }
}

/** §6 巡檢與除錯用：這個 session 觸發過的兜底。 */
export function ccpDenialLog(): readonly CcpDenialRecord[] {
    return denials;
}

function record(entry: CcpDenialRecord) {
    denials.push(entry);
    if (denials.length > RECORD_LIMIT) denials.shift();
    if (typeof window !== 'undefined') window.__ccpDenials = denials;
}

function present(notice: CcpDenialNotice, method: string, path: string) {
    // 同一個入口連按兩下不該疊出兩張一樣的 toast；`id` 讓 sonner 取代前一張。
    const id = `ccp-denial:${notice.kind}:${method}:${path}`;
    const description = notice.upstreamDetail
        // 上游原文**不是**我方文案：標示來源，純文字、已限長（denial.ts）。
        ? `${notice.message}（伺服器訊息：${notice.upstreamDetail}）`
        : notice.message;
    toast.error(notice.title, { id, description, duration: 8000 });
    console.warn(
        `[ccp] 403 兜底 kind=${notice.kind} gateway=${notice.fromGateway} ${method} ${path}`,
    );
    record({
        kind: notice.kind,
        method,
        path,
        fromGateway: notice.fromGateway,
        at: Date.now(),
    });
}

/** 跨來源的 403（遙測、外部整合）不是使用者按下的操作，不上說明面。 */
function sameOrigin(url: string): boolean {
    try {
        return new URL(url, window.location.href).origin === window.location.origin;
    } catch {
        return false;
    }
}

function requestUrl(input: RequestInfo | URL): string {
    if (typeof input === 'string') return input;
    if (input instanceof URL) return input.href;
    return input.url;
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
    if (init?.method) return init.method.toUpperCase();
    if (typeof input !== 'string' && !(input instanceof URL)) {
        return input.method.toUpperCase();
    }
    return 'GET';
}

let installed = false;

/**
 * 冪等：React strict mode 會把 effect 跑兩次，重複包裝會讓一次 403 出兩張 toast。
 */
export function installCcpDenialFallback() {
    if (installed || typeof window === 'undefined') return;
    installed = true;

    const original = window.fetch.bind(window);

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const response = await original(input, init);
        if (response.status !== 403) return response;

        const url = requestUrl(input);
        if (!sameOrigin(url)) return response;

        const method = requestMethod(input, init);
        const path = (() => {
            try {
                return new URL(url, window.location.href).pathname;
            } catch {
                return url;
            }
        })();

        // body 的讀取走複本且**不擋住回傳**：呼叫端拿到的是原封不動的 Response，
        // 兜底的說明晚幾毫秒出現不影響任何既有流程。
        const copy = response.clone();
        void copy
            .json()
            .catch(() => null)
            .then((body) => present(denialNoticeFromBody(body), method, path));

        return response;
    };
}
