"use client";

import { useState } from "react";

// customer-center-platform fork（母 repo W3b tasks 5.3／5.4／5.5／5.6）：
// transfer 工具表單重寫——**話術層 10 鍵全畫、部署層六欄只說明不畫**。
//
// 三格：
//   ① 話術層：messageType 三態（沿用）、兩條必填話術（帶理由）、afterHoursAction 下拉、
//      afterHoursMessage、unavailableAnnounceLimit（1–10）、timeout **唯讀＋說明**
//      （LiveKit 冷轉接不使用此值，母 repo review F-8）。
//   ② schedule 結構化編輯器：tz 下拉自 CCP_TZ_NAMES、七日多段、跨午夜標示、即時形狀驗證。
//      MUST NOT 用自由 JSON——執行層 is_open() 對不可解析的週表 fail-open（營業時間閘消失）。
//   ③ 部署層唯讀區塊：六欄名＋「由部署層供給」＋分角色程序；**不畫輸入框、不顯示值**
//      （GET 回讀到的是 W3a 清除後的 null，且 GET 不 merge 部署層）。
//
// 送出形狀由 `lib/ccp/transfer-call-config.ts` 的 builder 負責（本元件只管畫面與狀態）。
// 可及性契約沿用 W2d：`data-ccp-readonly`／`data-ccp-disabled` ＋ `aria-describedby`
// 指得到（頁面級說明條）或自帶 `title`。
import type { RecordingResponseSchema } from "@/client/types.gen";
import { RecordingSelect, StaticTextWarning } from "@/components/flow/TextOrAudioInput";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { CcpRole } from "@/lib/ccp/access";
import {
    CCP_TRANSFER_AFTER_HOURS_ACTIONS,
    CCP_TRANSFER_ANNOUNCE_LIMIT_RANGE,
    CCP_TRANSFER_DEPLOYMENT_KEYS,
    CCP_TZ_NAMES,
} from "@/lib/ccp/feature-scope";
import { CCP_ACCESS_NOTICE_ID, ccpDisabledProps, ccpReadOnlyFieldProps } from "@/lib/ccp/notice-bar";
import {
    type AfterHoursAction,
    DAY_KEYS,
    type DayKey,
    type FieldProblem,
    type Segment,
    segmentIsEmpty,
    segmentWrapsMidnight,
    toolFunctionName,
    type TransferFormState,
    type TransferMessageType,
    type WeeklySchedule,
} from "@/lib/ccp/transfer-call-config";

export interface TransferCallToolConfigProps {
    name: string;
    onNameChange: (name: string) => void;
    description: string;
    onDescriptionChange: (description: string) => void;
    form: TransferFormState;
    onFormChange: (next: TransferFormState) => void;
    recordings?: RecordingResponseSchema[];
    /** 主管（唯讀角色）時 true：話術層欄位全停用。 */
    readOnly: boolean;
    /** 已確認的角色；說明文案分角色。 */
    role: CcpRole | null;
    /** 表單驗證結果（頁面在存檔時算；即時形狀驗證由本元件自算）。 */
    problems?: FieldProblem[];
}

const AFTER_HOURS_LABELS: Record<AfterHoursAction, string> = {
    back_to_ai: "回到 AI 繼續服務（播完說明後不轉接）",
    announce_and_hangup: "播完說明後結束通話",
    alternate_queue: "轉接到替代隊列（目的地由部署層供給）",
};

const DAY_LABELS: Record<DayKey, string> = {
    mon: "週一", tue: "週二", wed: "週三", thu: "週四", fri: "週五", sat: "週六", sun: "週日",
};

const DEPLOYMENT_KEY_LABELS: Record<(typeof CCP_TRANSFER_DEPLOYMENT_KEYS)[number], string> = {
    destination: "轉接目的地",
    alternateDestination: "非營業時間替代目的地",
    queueHealthUrl: "隊列健康端點",
    queueHealthToken: "隊列健康端點憑證",
    queueHealthTimeoutSeconds: "健康探測逾時秒數",
    queueHealthCacheTtlSeconds: "健康判定快取秒數",
};

function problemFor(problems: FieldProblem[] | undefined, field: string): string | undefined {
    return problems?.find((p) => p.field === field)?.message;
}

export function TransferCallToolConfig({
    name,
    onNameChange,
    description,
    onDescriptionChange,
    form,
    onFormChange,
    recordings = [],
    readOnly,
    role,
    problems,
}: TransferCallToolConfigProps) {
    const set = <K extends keyof TransferFormState>(key: K, value: TransferFormState[K]) =>
        onFormChange({ ...form, [key]: value });
    // 可及性契約（W2d G1）：`aria-describedby` 要指得到，否則自帶 `title`。頁面級說明條
    // 只在角色**已確認**時渲染（loading／signal-unavailable 回 null），故 role 未到時
    // 不指、改帶 title（ui review M-6）。
    const notice = role ? CCP_ACCESS_NOTICE_ID : undefined;
    const ro = {
        ...ccpReadOnlyFieldProps(readOnly, role ? {} : { describedBy: null }),
        ...(readOnly && !role ? { title: "尚未取得權限訊號，暫以唯讀呈現" } : {}),
    };
    const [limitMin, limitMax] = CCP_TRANSFER_ANNOUNCE_LIMIT_RANGE;

    return (
        <Card>
            <CardHeader>
                <CardTitle>轉接工具設定</CardTitle>
                <CardDescription>
                    話術與營業時間在此維護；轉接目的地與隊列參數由部署層供給（見最下方）。
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
                <div className="grid gap-2">
                    <Label htmlFor="transfer-name">工具名稱</Label>
                    <Label className="text-xs text-muted-foreground">
                        給 AI 辨識用的名稱。AI 端只保留 a-z、0-9 與底線——請用英數字命名；中文會被整個去掉。
                    </Label>
                    <Input
                        id="transfer-name"
                        value={name}
                        onChange={(e) => onNameChange(e.target.value)}
                        aria-invalid={problemFor(problems, "name") ? true : undefined}
                        aria-describedby={problemFor(problems, "name") ? "transfer-name-error" : "transfer-name-fn"}
                        {...ro}
                    />
                    {/* security review H-1：就地顯示 AI 實際看到的 function name；為空時前端擋下（閘門同批擋）。 */}
                    <Label id="transfer-name-fn" className={`text-xs ${toolFunctionName(name) ? "text-muted-foreground" : "text-red-600"}`}>
                        AI 實際看到的名稱：{toolFunctionName(name) ? <code>{toolFunctionName(name)}</code> : "（空——AI 將叫不到這支工具）"}
                    </Label>
                    {problemFor(problems, "name") && (
                        <Label id="transfer-name-error" role="alert" className="text-xs text-red-500">{problemFor(problems, "name")}</Label>
                    )}
                </div>

                <div className="grid gap-2">
                    <Label htmlFor="transfer-description">說明</Label>
                    <Label className="text-xs text-muted-foreground">幫助 AI 判斷什麼時候該轉接真人</Label>
                    <Textarea
                        id="transfer-description"
                        value={description}
                        onChange={(e) => onDescriptionChange(e.target.value)}
                        rows={3}
                        {...ro}
                    />
                </div>

                {/* ── ① 話術層 ─────────────────────────────────────────────── */}
                <div className="grid gap-4 pt-4 border-t">
                    <Label>轉接前的播報</Label>
                    <Label className="text-xs text-muted-foreground">
                        轉接真人之前要不要先對來電者說一句話。切換播報方式後存檔，另一種方式的內容（文字或錄音）不會保留。
                    </Label>
                    <RadioGroup
                        value={form.messageType}
                        onValueChange={(v) => set("messageType", v as TransferMessageType)}
                        className="space-y-3"
                        aria-readonly={readOnly || undefined}
                        aria-describedby={readOnly ? notice : undefined}
                        data-ccp-readonly={readOnly ? "true" : undefined}
                    >
                        <label htmlFor="transfer-mt-none" className="flex items-center space-x-3 p-3 border rounded-lg hover:bg-muted/50 cursor-pointer">
                            <RadioGroupItem value="none" id="transfer-mt-none" disabled={readOnly} title={readOnly && !role ? "尚未取得權限訊號，暫以唯讀呈現" : undefined} />
                            <div className="flex-1">
                                <span className="font-medium">不播報</span>
                                <p className="text-xs text-muted-foreground">直接轉接</p>
                            </div>
                        </label>
                        <div className="flex items-start space-x-3 p-3 border rounded-lg hover:bg-muted/50">
                            <RadioGroupItem value="custom" id="transfer-mt-custom" className="mt-1" disabled={readOnly} />
                            <label htmlFor="transfer-mt-custom" className="flex-1 space-y-2 cursor-pointer">
                                <span className="font-medium">播報文字</span>
                                <p className="text-xs text-muted-foreground">由語音合成朗讀一段文字</p>
                            </label>
                        </div>
                        {form.messageType === "custom" && (
                            <div className="pl-8 space-y-2">
                                <StaticTextWarning />
                                <Textarea
                                    aria-label="轉接前播報文字"
                                    value={form.customMessage}
                                    onChange={(e) => set("customMessage", e.target.value)}
                                    placeholder="例：好的，正在為您轉接真人客服，請稍候。"
                                    rows={2}
                                    {...ro}
                                />
                            </div>
                        )}
                        <div className="flex items-start space-x-3 p-3 border rounded-lg hover:bg-muted/50">
                            <RadioGroupItem value="audio" id="transfer-mt-audio" className="mt-1" disabled={readOnly} />
                            <label htmlFor="transfer-mt-audio" className="flex-1 space-y-2 cursor-pointer">
                                <span className="font-medium">播放錄音</span>
                                <p className="text-xs text-muted-foreground">播放一段預先錄好的音檔</p>
                            </label>
                        </div>
                        {form.messageType === "audio" && (
                            <div className="pl-8">
                                {readOnly ? (
                                    // ui review H-1：`RecordingSelect` 沒有 disabled 接點（TextOrAudioInput.tsx），
                                    // 唯讀態改畫唯讀欄位——主管可看到選了哪一段錄音，但改不了。
                                    <Input
                                        aria-label="轉接前播放的錄音"
                                        value={
                                            (() => {
                                                const r = recordings.find((x) => String(x.recording_id) === form.audioRecordingId || String(x.id) === form.audioRecordingId);
                                                return r ? (r.transcript || r.recording_id) : (form.audioRecordingId || "（未選擇）");
                                            })()
                                        }
                                        onChange={() => undefined}
                                        {...ro}
                                    />
                                ) : (
                                    <RecordingSelect
                                        value={form.audioRecordingId}
                                        onChange={(id) => set("audioRecordingId", id)}
                                        recordings={recordings}
                                    />
                                )}
                            </div>
                        )}
                    </RadioGroup>
                </div>

                <div className="grid gap-2 pt-4 border-t">
                    <Label htmlFor="transfer-failed-message">轉接失敗時的說明 <span className="text-red-500">*</span></Label>
                    <Label className="text-xs text-muted-foreground">
                        真人沒有接起時來電者聽到的話。必填——來電者不能聽到一片安靜。
                    </Label>
                    <Textarea
                        id="transfer-failed-message"
                        value={form.transferFailedMessage}
                        onChange={(e) => set("transferFailedMessage", e.target.value)}
                        rows={2}
                        aria-invalid={problemFor(problems, "transferFailedMessage") ? true : undefined}
                        aria-describedby={problemFor(problems, "transferFailedMessage") ? "transfer-failed-message-error" : ro["aria-describedby"]}
                        {...ro}
                    />
                    {problemFor(problems, "transferFailedMessage") && (
                        <Label id="transfer-failed-message-error" role="alert" className="text-xs text-red-500">{problemFor(problems, "transferFailedMessage")}</Label>
                    )}
                </div>

                <div className="grid gap-2">
                    <Label htmlFor="transfer-unavailable-message">真人無法接聽時的說明 <span className="text-red-500">*</span></Label>
                    <Label className="text-xs text-muted-foreground">
                        隊列不健康（無人上線、系統異常）時來電者聽到的話。必填。
                    </Label>
                    <Textarea
                        id="transfer-unavailable-message"
                        value={form.transferUnavailableMessage}
                        onChange={(e) => set("transferUnavailableMessage", e.target.value)}
                        rows={2}
                        aria-invalid={problemFor(problems, "transferUnavailableMessage") ? true : undefined}
                        aria-describedby={problemFor(problems, "transferUnavailableMessage") ? "transfer-unavailable-message-error" : ro["aria-describedby"]}
                        {...ro}
                    />
                    {problemFor(problems, "transferUnavailableMessage") && (
                        <Label id="transfer-unavailable-message-error" role="alert" className="text-xs text-red-500">{problemFor(problems, "transferUnavailableMessage")}</Label>
                    )}
                </div>

                <div className="grid gap-2">
                    <Label htmlFor="transfer-announce-limit">無法接聽時的播報次數上限</Label>
                    <Label className="text-xs text-muted-foreground">
                        同一通電話最多播報幾次「無法接聽」後就結束（{limitMin}–{limitMax}）。留空＝用系統預設。
                    </Label>
                    <Input
                        id="transfer-announce-limit"
                        type="number"
                        min={limitMin}
                        max={limitMax}
                        step={1}
                        className="w-32"
                        value={form.unavailableAnnounceLimit ?? ""}
                        onChange={(e) => {
                            const raw = e.target.value;
                            set("unavailableAnnounceLimit", raw === "" ? null : Number(raw));
                        }}
                        aria-invalid={problemFor(problems, "unavailableAnnounceLimit") ? true : undefined}
                        aria-describedby={problemFor(problems, "unavailableAnnounceLimit") ? "transfer-announce-limit-error" : ro["aria-describedby"]}
                        {...ro}
                    />
                    {problemFor(problems, "unavailableAnnounceLimit") && (
                        <Label id="transfer-announce-limit-error" role="alert" className="text-xs text-red-500">{problemFor(problems, "unavailableAnnounceLimit")}</Label>
                    )}
                </div>

                <div className="grid gap-2 pt-4 border-t">
                    <Label>非營業時間的行為</Label>
                    <Label className="text-xs text-muted-foreground">
                        依下方營業時間表判定為非營業時間時，來電者要求真人怎麼處理（未設定時執行層＝回到 AI）。
                    </Label>
                    {/* ui review M-8：未設定與 back_to_ai 在執行層同結果，不畫兩個同義選項。
                        未設時顯示 back_to_ai；使用者未動過就仍送 null（builder 送現值）。 */}
                    <Select
                        value={form.afterHoursAction || "back_to_ai"}
                        onValueChange={(v) => set("afterHoursAction", v as AfterHoursAction)}
                        disabled={readOnly}
                    >
                        <SelectTrigger aria-label="非營業時間的行為" aria-describedby={readOnly ? notice : undefined} data-ccp-readonly={readOnly ? "true" : undefined}>
                            <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                            {CCP_TRANSFER_AFTER_HOURS_ACTIONS.map((a) => (
                                <SelectItem key={a} value={a}>{AFTER_HOURS_LABELS[a]}</SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                    {form.afterHoursAction === "alternate_queue" && (
                        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
                            替代隊列的目的地由部署層供給（{DEPLOYMENT_KEY_LABELS.alternateDestination}），
                            不在此設定；部署層未供給時執行期會降級為回到 AI。
                        </p>
                    )}
                </div>

                <div className="grid gap-2">
                    <Label htmlFor="transfer-after-hours-message">非營業時間的說明</Label>
                    <Label className="text-xs text-muted-foreground">非營業時間來電者要求真人時聽到的話。留空＝用系統預設句。</Label>
                    <Textarea
                        id="transfer-after-hours-message"
                        value={form.afterHoursMessage}
                        onChange={(e) => set("afterHoursMessage", e.target.value)}
                        rows={2}
                        {...ro}
                    />
                </div>

                {/* ── ② 營業時間表 ──────────────────────────────────────────── */}
                <ScheduleEditor
                    schedule={form.schedule}
                    onChange={(s) => set("schedule", s)}
                    readOnly={readOnly}
                    problems={problems}
                    notice={notice}
                    ro={ro}
                />

                <div className="grid gap-2 pt-4 border-t">
                    <Label>等待接聽秒數（本部署不使用）</Label>
                    <Label className="text-xs text-muted-foreground" id="transfer-timeout-why">
                        本部署以 LiveKit 冷轉接送出 REFER 後即離線，不等待目的地接聽，故此值不生效。
                        這裡如實顯示現值，存檔會原樣帶回（整份取代），但無法在此調整。
                    </Label>
                    {/* ui review H-2：spec 的判準在**呈現面**——一個 `<input type=number>` 就算 readOnly
                        也長得像可調（hover 出 spinner）。改為純文字現值，不畫輸入框。 */}
                    <p
                        id="transfer-timeout"
                        className="w-32 rounded-md border border-dashed px-3 py-2 text-sm text-muted-foreground"
                        aria-describedby="transfer-timeout-why"
                        data-ccp-readonly="true"
                        title="LiveKit 冷轉接不使用此值"
                    >
                        {form.timeout} 秒
                    </p>
                </div>

                {/* ── ③ 部署層唯讀區塊 ─────────────────────────────────────── */}
                <div className="grid gap-2 pt-4 border-t" data-ccp-deployment-layer="true">
                    <Label>由部署層供給的設定</Label>
                    <Label className="text-xs text-muted-foreground">
                        下列項目不屬於這份工具設定，值不在此顯示、也不由編輯器寫入
                        （寫入會被內容檢查擋下）。
                    </Label>
                    <ul className="text-sm divide-y rounded-lg border">
                        {CCP_TRANSFER_DEPLOYMENT_KEYS.map((k) => (
                            <li key={k} className="flex items-center justify-between px-3 py-2" data-ccp-deployment-key={k}>
                                <span>{DEPLOYMENT_KEY_LABELS[k]}</span>
                                <span className="text-xs text-muted-foreground" title={k}>由部署層供給</span>
                            </li>
                        ))}
                    </ul>
                    <p className="text-xs text-muted-foreground">
                        {role === "implementer"
                            ? "變更程序：依 deploy/RUNBOOK.md 的轉接設定程序修改部署層環境變數後重新部署；preflight 會對值做形狀與白名單檢查。"
                            : "需要調整時，請與您的專案窗口提出，由建置單位在部署層變更。"}
                    </p>
                </div>
            </CardContent>
        </Card>
    );
}

// ── 營業時間表結構化編輯器（task 5.4）────────────────────────────────────

interface ScheduleEditorProps {
    schedule: WeeklySchedule | null;
    onChange: (next: WeeklySchedule | null) => void;
    readOnly: boolean;
    problems?: FieldProblem[];
    /** 唯讀態 `aria-describedby` 的目標（角色未確認時為 undefined → 改帶 title）。 */
    notice: string | undefined;
    ro: ReturnType<typeof ccpReadOnlyFieldProps> & { title?: string };
}

function ScheduleEditor({ schedule, onChange, readOnly, problems, notice, ro }: ScheduleEditorProps) {
    const enabled = schedule !== null;
    // review F-18：485 個時區一次全畫沒有搜尋；加一個純前端篩選（不改判準，選項仍只來自快照）。
    const [tzFilter, setTzFilter] = useState("");
    const tzOptions = tzFilter
        ? CCP_TZ_NAMES.filter((tz) => tz.toLowerCase().includes(tzFilter.toLowerCase()))
        : CCP_TZ_NAMES;
    // review F-2：不認得的鍵（舊形狀、上游新增子鍵）——畫面上明說，不靜默丟。
    const unknownKeys = enabled && schedule && typeof schedule === "object"
        ? Object.keys(schedule).filter((k) => k !== "tz" && !(DAY_KEYS as readonly string[]).includes(k))
        : [];
    const tzProblem = problemFor(problems, "schedule.tz");

    const update = (patch: Partial<WeeklySchedule>) => onChange({ ...(schedule ?? {}), ...patch });
    const anyDayListed = !!schedule && DAY_KEYS.some((d) => schedule[d] !== undefined);
    const setSegments = (day: DayKey, segs: Segment[]) => update({ [day]: segs });

    return (
        <div className="grid gap-3 pt-4 border-t" data-ccp-schedule-editor="true">
            <div className="flex items-center justify-between">
                <div>
                    <Label>營業時間表</Label>
                    <Label className="block text-xs text-muted-foreground">
                        未設定＝全天候可轉接。設定後，表外時間走上方「非營業時間的行為」。
                    </Label>
                </div>
                {enabled ? (
                    <Button type="button" variant="outline" size="sm" onClick={() => onChange(null)} {...ccpDisabledProps(readOnly)}>
                        清除時間表
                    </Button>
                ) : (
                    <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => onChange({ tz: "Asia/Taipei", mon: [["09:00", "18:00"]], tue: [["09:00", "18:00"]], wed: [["09:00", "18:00"]], thu: [["09:00", "18:00"]], fri: [["09:00", "18:00"]] })}
                        {...ccpDisabledProps(readOnly)}
                    >
                        設定時間表
                    </Button>
                )}
            </div>

            {enabled && (
                <div className="grid gap-3 rounded-lg border p-3">
                    {unknownKeys.length > 0 && (
                        <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2" role="alert" data-ccp-schedule-unknown-keys="true">
                            既有的營業時間表含本部署不認得的鍵：{unknownKeys.map((k) => k.slice(0, 40)).join("、")}。
                            這份表原樣保留，存檔會被擋下；請「清除時間表」後重新設定，或先自版控移除這些鍵。
                        </p>
                    )}
                    <div className="grid gap-1">
                        <Label htmlFor="transfer-schedule-tz">時區</Label>
                        <Input
                            aria-label="篩選時區"
                            placeholder="輸入關鍵字篩選（例：Taipei、Tokyo）"
                            className="w-72"
                            value={tzFilter}
                            onChange={(e) => setTzFilter(e.target.value)}
                            {...ro}
                        />
                        <Select value={schedule.tz ?? ""} onValueChange={(v) => update({ tz: v })} disabled={readOnly}>
                            <SelectTrigger id="transfer-schedule-tz" className="w-72" aria-invalid={tzProblem ? true : undefined} aria-describedby={readOnly ? notice : undefined} data-ccp-readonly={readOnly ? "true" : undefined}>
                                <SelectValue placeholder="選擇時區" />
                            </SelectTrigger>
                            <SelectContent className="max-h-72">
                                {schedule.tz && !tzOptions.includes(schedule.tz as (typeof CCP_TZ_NAMES)[number]) && (
                                    <SelectItem value={schedule.tz}>{schedule.tz}</SelectItem>
                                )}
                                {tzOptions.map((tz) => (
                                    <SelectItem key={tz} value={tz}>{tz}</SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                        {tzProblem && <Label className="text-xs text-red-500" role="alert">{tzProblem}</Label>}
                        {!schedule.tz && (
                            <Label className="text-xs text-red-600" data-ccp-schedule-no-tz="true">
                                尚未指定時區：執行層會以 UTC 判讀整張表（沒有「系統預設」），存檔會被擋下。
                            </Label>
                        )}
                    </div>

                    {/* ui review M-1：執行層（business_hours.py）——沒有任何一天列出＝整份等同未設定（全天候
                        開放）；有列出任何一天後，未列出的日子＝當天不營業（前一日跨午夜的段除外）。兩種情形
                        畫面本來完全相同而結果相反，這裡把它說出來。 */}
                    {!anyDayListed && (
                        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2" data-ccp-schedule-empty="true">
                            目前沒有列出任何一天：這份時間表在執行層等同「未設定」（全天候可轉接）。請至少為一天加時段。
                        </p>
                    )}
                    {DAY_KEYS.map((day) => {
                        const segs = schedule[day] ?? [];
                        const defined = schedule[day] !== undefined;
                        return (
                            <div key={day} className="grid gap-1" data-ccp-schedule-day={day}>
                                <div className="flex items-center gap-3">
                                    <span className="w-10 text-sm font-medium">{DAY_LABELS[day]}</span>
                                    {!defined && anyDayListed && <span className="text-xs text-muted-foreground">未列出＝當天不營業（前一日跨午夜的時段除外）</span>}
                                    {!defined && !anyDayListed && <span className="text-xs text-muted-foreground">未列出</span>}
                                    {defined && segs.length === 0 && <span className="text-xs text-muted-foreground">當天休息</span>}
                                    <Button
                                        type="button"
                                        variant="ghost"
                                        size="sm"
                                        onClick={() => setSegments(day, [...segs, ["09:00", "18:00"]])}
                                        {...ccpDisabledProps(readOnly)}
                                    >
                                        ＋ 時段
                                    </Button>
                                    {defined && (
                                        <Button
                                            type="button"
                                            variant="ghost"
                                            size="sm"
                                            onClick={() => {
                                                const next = { ...schedule };
                                                delete next[day];
                                                onChange(next);
                                            }}
                                            {...ccpDisabledProps(readOnly)}
                                        >
                                            移除整天
                                        </Button>
                                    )}
                                </div>
                                {segs.map((seg, i) => {
                                    const problem = problemFor(problems, `schedule.${day}[${i}]`);
                                    return (
                                        <div key={i} className="ml-10 flex flex-wrap items-center gap-2" data-ccp-schedule-segment={`${day}[${i}]`}>
                                            <Input
                                                type="time"
                                                step={60}
                                                className="w-32"
                                                aria-label={`${DAY_LABELS[day]} 第 ${i + 1} 段開始`}
                                                value={seg[0]}
                                                onChange={(e) => setSegments(day, segs.map((s, j) => (j === i ? [e.target.value, s[1]] : s)))}
                                                aria-invalid={problem ? true : undefined}
                                                {...ro}
                                            />
                                            <span className="text-sm">到</span>
                                            <Input
                                                type="time"
                                                step={60}
                                                className="w-32"
                                                aria-label={`${DAY_LABELS[day]} 第 ${i + 1} 段結束`}
                                                value={seg[1]}
                                                onChange={(e) => setSegments(day, segs.map((s, j) => (j === i ? [s[0], e.target.value] : s)))}
                                                aria-invalid={problem ? true : undefined}
                                                {...ro}
                                            />
                                            {segmentWrapsMidnight(seg) && (
                                                <span className="text-xs text-amber-700" data-ccp-wraps-midnight="true">跨午夜（到隔天）</span>
                                            )}
                                            {segmentIsEmpty(seg) && (
                                                <span className="text-xs text-red-600" data-ccp-empty-segment="true">開始與結束相同＝空時段（執行層視為當天此段不營業）；全天請用 00:00–23:59</span>
                                            )}
                                            <Button
                                                type="button"
                                                variant="ghost"
                                                size="sm"
                                                aria-label={`移除 ${DAY_LABELS[day]} 第 ${i + 1} 段`}
                                                onClick={() => setSegments(day, segs.filter((_, j) => j !== i))}
                                                {...ccpDisabledProps(readOnly)}
                                            >
                                                ×
                                            </Button>
                                            {problem && <Label className="basis-full text-xs text-red-500">{problem}</Label>}
                                        </div>
                                    );
                                })}
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}
