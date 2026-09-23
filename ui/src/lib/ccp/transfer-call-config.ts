/**
 * customer-center-platform fork（母 repo W3b task 5.1）：transfer 工具**話術層**的
 * 送出形狀 builder ＋ 表單驗證器。純函式，零 React 依賴——`scripts/transfer-call-config.test.mts`
 * 以 node 直接跑。
 *
 * ## 契約（母 repo design D1）
 *
 * - PUT body 的 `config` ＝ 話術層 **10 鍵全帶**，**永不含**部署層六欄——即使值是回讀到的
 *   `null`（閘門 `forbidden_keys` 是**存在性**判定）。
 * - `messageType`／`timeout` 非 Optional：帶表單現值（回讀缺席時用 schema 預設 `"none"`／`30`），
 *   MUST NOT 送 `null`。其餘 8 鍵 Optional，未設者送 `null`。
 * - **不是「只送改過的欄位」**：上游 `update_tool` 對 definition 是整份取代 ＋ seed-once，
 *   漏送一鍵＝永久清空（W2c CS-21 的「存一次檔丟 9 鍵」）。
 * - builder 以**字面量**構造、MUST NOT `{...tool.definition.config}` 展開（review F-18：
 *   excess property check 只作用於字面量；展開會把六欄原樣帶回去）。
 *
 * ## 編譯期斷言守的是**鍵集合**，不是值
 *
 * `Required<Omit<…>>` 使漏寫一鍵 `tsc` 紅；`& keyof TransferCallConfig` 使打錯部署層鍵名紅；
 * `_Exhaustive` 使上游新增任一鍵（重生成後）紅。值的正確性（六欄絕不出現、非 Optional 鍵非 null）
 * 靠 5.2 的執行期向量——誠實記載（review M-10）。
 *
 * ## 前端驗證是體驗面，不是控制面
 *
 * 本檔判定的每一項（必填非空、列舉、範圍、週表形狀）在閘門都有對應規則
 * （`required_keys` ＋ `constrained_values` 四種 kind）。`timeout` **不判定**：
 * 它在本部署（LiveKit 冷轉接）不生效，表單呈現為唯讀（review F-8）。
 */

import type { TransferCallConfig } from '@/client/types.gen';

import {
    CCP_TRANSFER_AFTER_HOURS_ACTIONS,
    CCP_TRANSFER_ANNOUNCE_LIMIT_RANGE,
    CCP_TRANSFER_DEPLOYMENT_KEYS,
    CCP_TRANSFER_MESSAGE_TYPES,
    CCP_TZ_NAMES,
} from './feature-scope.ts';

export type DeploymentKeys = (typeof CCP_TRANSFER_DEPLOYMENT_KEYS)[number] & keyof TransferCallConfig;
/** 話術層 10 鍵，每鍵**必出現**（值可為 null）。 */
export type ScriptLayer = Required<Omit<TransferCallConfig, DeploymentKeys>>;
// 上游新增任一鍵（重生成後）這裡就編不過——那時要決定它歸哪一層，不是靜默漏送。
type _Exhaustive = Exclude<keyof TransferCallConfig, DeploymentKeys | keyof ScriptLayer> extends never ? true : never;
const _exhaustive: _Exhaustive = true;
void _exhaustive;

export type AfterHoursAction = (typeof CCP_TRANSFER_AFTER_HOURS_ACTIONS)[number];
export type TransferMessageType = (typeof CCP_TRANSFER_MESSAGE_TYPES)[number];
export const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const;
export type DayKey = (typeof DAY_KEYS)[number];
export type Segment = readonly [string, string];

/** 結構化週表（`business_hours.py` docstring 的形狀）。`tz` 可缺席（執行層預設）。 */
export interface WeeklySchedule {
    tz?: string;
    mon?: Segment[]; tue?: Segment[]; wed?: Segment[]; thu?: Segment[];
    fri?: Segment[]; sat?: Segment[]; sun?: Segment[];
}

/** 表單狀態＝話術層 10 鍵的可編輯形狀。 */
export interface TransferFormState {
    messageType: TransferMessageType;
    customMessage: string;
    audioRecordingId: string;
    timeout: number;
    schedule: WeeklySchedule | null;
    afterHoursAction: AfterHoursAction | '';
    afterHoursMessage: string;
    transferFailedMessage: string;
    transferUnavailableMessage: string;
    unavailableAnnounceLimit: number | null;
}

export const SCHEMA_DEFAULT_MESSAGE_TYPE: TransferMessageType = 'none';
export const SCHEMA_DEFAULT_TIMEOUT = 30;

const HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/** 回讀 → 表單狀態。**只取話術層鍵**，六欄不讀（表單不畫、不顯示值）。 */
export function formStateFromConfig(config: TransferCallConfig | null | undefined): TransferFormState {
    const c = config ?? {};
    return {
        messageType: (CCP_TRANSFER_MESSAGE_TYPES as readonly string[]).includes(c.messageType ?? '')
            ? (c.messageType as TransferMessageType)
            : SCHEMA_DEFAULT_MESSAGE_TYPE,
        customMessage: c.customMessage ?? '',
        audioRecordingId: c.audioRecordingId ?? '',
        timeout: typeof c.timeout === 'number' ? c.timeout : SCHEMA_DEFAULT_TIMEOUT,
        schedule: isWeeklyScheduleShape(c.schedule) ? (c.schedule as WeeklySchedule) : c.schedule ? { } : null,
        afterHoursAction: (CCP_TRANSFER_AFTER_HOURS_ACTIONS as readonly string[]).includes(c.afterHoursAction ?? '')
            ? (c.afterHoursAction as AfterHoursAction)
            : '',
        afterHoursMessage: c.afterHoursMessage ?? '',
        transferFailedMessage: c.transferFailedMessage ?? '',
        transferUnavailableMessage: c.transferUnavailableMessage ?? '',
        unavailableAnnounceLimit: typeof c.unavailableAnnounceLimit === 'number' ? c.unavailableAnnounceLimit : null,
    };
}

function isWeeklyScheduleShape(value: unknown): boolean {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    return Object.keys(value).every((k) => k === 'tz' || (DAY_KEYS as readonly string[]).includes(k));
}

/**
 * 表單狀態 → PUT 的 `config`。**字面量逐鍵構造**；六欄任一鍵在這裡結構上寫不進去
 * （`ScriptLayer` 不含它們，多寫會 `tsc` 紅），且 10 鍵全帶。
 */
export function buildTransferCallConfig(s: TransferFormState): ScriptLayer {
    return {
        messageType: s.messageType || SCHEMA_DEFAULT_MESSAGE_TYPE,
        customMessage: s.messageType === 'custom' ? (s.customMessage || null) : null,
        audioRecordingId: s.messageType === 'audio' ? (s.audioRecordingId || null) : null,
        timeout: Number.isInteger(s.timeout) ? s.timeout : SCHEMA_DEFAULT_TIMEOUT,
        schedule: s.schedule && Object.keys(s.schedule).length > 0 ? normalizeSchedule(s.schedule) : null,
        afterHoursAction: s.afterHoursAction || null,
        afterHoursMessage: s.afterHoursMessage || null,
        transferFailedMessage: s.transferFailedMessage || null,
        transferUnavailableMessage: s.transferUnavailableMessage || null,
        unavailableAnnounceLimit: s.unavailableAnnounceLimit ?? null,
    };
}

function normalizeSchedule(sched: WeeklySchedule): Record<string, unknown> {
    // 只保留有意義的鍵：tz（有值才帶）、有段的日；空段的日也保留（正本判合法，且它表達「當天休息」）。
    const out: Record<string, unknown> = {};
    if (sched.tz) out.tz = sched.tz;
    for (const day of DAY_KEYS) {
        const segs = sched[day];
        if (segs !== undefined) out[day] = segs.map(([a, b]) => [a, b]);
    }
    return out;
}

export interface FieldProblem {
    field: keyof TransferFormState | `schedule.${string}`;
    message: string;
}

/**
 * 表單驗證。每一項在閘門都有對應規則（**不得比閘門更嚴**，也不靠上游）：
 * 必填非空（`required_keys`）、兩條列舉（`enum`）、播報次數（`int_range` 1–10）、
 * 週表形狀（`weekly_schedule`：tz ∈ 快照、日鍵、`HH:MM` 段）。`timeout` 不驗。
 */
export function validateScriptLayer(s: TransferFormState): FieldProblem[] {
    const problems: FieldProblem[] = [];
    if (!s.transferFailedMessage.trim()) {
        problems.push({ field: 'transferFailedMessage', message: '轉接失敗時的說明為必填（來電者不能聽到一片安靜）' });
    }
    if (!s.transferUnavailableMessage.trim()) {
        problems.push({ field: 'transferUnavailableMessage', message: '真人無法接聽時的說明為必填（來電者不能聽到一片安靜）' });
    }
    if (!(CCP_TRANSFER_MESSAGE_TYPES as readonly string[]).includes(s.messageType)) {
        problems.push({ field: 'messageType', message: '轉接前播報方式不在允許的選項內' });
    }
    if (s.afterHoursAction && !(CCP_TRANSFER_AFTER_HOURS_ACTIONS as readonly string[]).includes(s.afterHoursAction)) {
        problems.push({ field: 'afterHoursAction', message: '非營業時間行為不在允許的選項內' });
    }
    if (s.unavailableAnnounceLimit !== null) {
        const [lo, hi] = CCP_TRANSFER_ANNOUNCE_LIMIT_RANGE;
        const v = s.unavailableAnnounceLimit;
        if (!Number.isInteger(v) || v < lo || v > hi) {
            problems.push({ field: 'unavailableAnnounceLimit', message: `播報次數上限須為 ${lo}–${hi} 的整數` });
        }
    }
    if (s.schedule) problems.push(...validateSchedule(s.schedule));
    return problems;
}

export function validateSchedule(sched: WeeklySchedule): FieldProblem[] {
    const problems: FieldProblem[] = [];
    for (const key of Object.keys(sched)) {
        if (key === 'tz') {
            const tz = sched.tz;
            if (tz && !(CCP_TZ_NAMES as readonly string[]).includes(tz)) {
                problems.push({ field: 'schedule.tz', message: '時區不在本部署認得的清單內' });
            }
            continue;
        }
        if (!(DAY_KEYS as readonly string[]).includes(key)) {
            problems.push({ field: `schedule.${key}`, message: '未知的星期鍵' });
            continue;
        }
        const segs = sched[key as DayKey] ?? [];
        segs.forEach((seg, i) => {
            const ok = Array.isArray(seg) && seg.length === 2 && seg.every((t) => typeof t === 'string' && HHMM_RE.test(t));
            if (!ok) problems.push({ field: `schedule.${key}[${i}]`, message: '時段須為兩個 HH:MM（24 小時制）' });
        });
    }
    return problems;
}

/** 跨午夜（結束 **<** 開始）的段：執行層合法，畫面上標示以免被當成打錯。 */
export function segmentWrapsMidnight([start, end]: Segment): boolean {
    return HHMM_RE.test(start) && HHMM_RE.test(end) && end < start;
}

/** 開始＝結束：執行層 `business_hours.py` 明文「empty interval — never open」，不是跨午夜（ui review H-3）。 */
export function segmentIsEmpty([start, end]: Segment): boolean {
    return HHMM_RE.test(start) && HHMM_RE.test(end) && end === start;
}
