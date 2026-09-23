// customer-center-platform fork（母 repo W3b task 5.2）：
// `lib/ccp/transfer-call-config.ts` 的執行期向量。跑法同 ccp-access-rules（Node 24+ 或 node:24 容器）。
//
// 守的是 `tsc` 守不到的那一半（設計 D1，誠實記載）：**值**——六欄任一鍵絕不出現
// （含回讀帶 null 的輸入）、10 鍵全在、`messageType`／`timeout` 非 null、週表正反例與
// 母 repo `test_feature_scope_check.py` 的 W3b 組同源、`{...spread}` 反例不採用。

import assert from "node:assert/strict";

import { CCP_TRANSFER_DEPLOYMENT_KEYS } from "../src/lib/ccp/feature-scope.ts";
import {
    buildTransferCallConfig,
    formStateFromConfig,
    segmentWrapsMidnight,
    validateScriptLayer,
} from "../src/lib/ccp/transfer-call-config.ts";

let passed = 0;
function check(name: string, fn: () => void) {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
}

const SCRIPT_KEYS = [
    "messageType", "customMessage", "audioRecordingId", "timeout", "schedule",
    "afterHoursAction", "afterHoursMessage", "transferFailedMessage",
    "transferUnavailableMessage", "unavailableAnnounceLimit",
].sort();

// 回讀形狀：W3a 後 DB 內六欄皆為顯式 null、鍵仍在（db_shapes.py）。
const READBACK = {
    destination: null, alternateDestination: null, queueHealthUrl: null, queueHealthToken: null,
    queueHealthTimeoutSeconds: null, queueHealthCacheTtlSeconds: null,
    messageType: "custom", customMessage: "好的，正在為您轉接真人客服，請稍候。", audioRecordingId: null,
    timeout: 30, schedule: null, afterHoursAction: "back_to_ai",
    afterHoursMessage: "目前是非營業時間。", transferFailedMessage: "轉接暫時沒有成功。",
    transferUnavailableMessage: "真人客服目前無法接聽。", unavailableAnnounceLimit: 2,
};
const GOOD_SCHEDULE = { tz: "Asia/Taipei", mon: [["09:00", "18:00"]], fri: [["22:00", "02:00"]], sat: [] };

console.log("transfer-call-config");

check("回讀帶 null 六欄 → 送出 body 不含六欄任一鍵，且 10 鍵全在", () => {
    const body = buildTransferCallConfig(formStateFromConfig(READBACK as never));
    for (const k of CCP_TRANSFER_DEPLOYMENT_KEYS) assert.ok(!(k in body), `不得含 ${k}`);
    assert.deepEqual(Object.keys(body).sort(), SCRIPT_KEYS);
});

check("回讀帶真值六欄（分層前的舊備份）→ 同樣不含", () => {
    const legacy = { ...READBACK, destination: "sip:q@pbx", queueHealthUrl: "http://queue:8080/h", queueHealthToken: "t" };
    const body = buildTransferCallConfig(formStateFromConfig(legacy as never));
    for (const k of CCP_TRANSFER_DEPLOYMENT_KEYS) assert.ok(!(k in body));
});

check("空回讀（config 缺席）→ messageType／timeout 帶 schema 預設而非 null", () => {
    const body = buildTransferCallConfig(formStateFromConfig(undefined));
    assert.equal(body.messageType, "none");
    assert.equal(body.timeout, 30);
    assert.deepEqual(Object.keys(body).sort(), SCRIPT_KEYS);
    for (const k of ["customMessage", "schedule", "afterHoursAction", "unavailableAnnounceLimit"]) {
        assert.equal((body as Record<string, unknown>)[k], null, `${k} 未設者送 null`);
    }
});

check("messageType 不在列舉 → 落回預設；不會把怪值送出去", () => {
    const s = formStateFromConfig({ ...READBACK, messageType: "voice" } as never);
    assert.equal(s.messageType, "none");
});

check("customMessage 只在 messageType=custom 時送；audio 同理", () => {
    const s = formStateFromConfig(READBACK as never);
    s.messageType = "audio"; s.audioRecordingId = "42";
    const body = buildTransferCallConfig(s);
    assert.equal(body.customMessage, null);
    assert.equal(body.audioRecordingId, "42");
});

check("合法週表原樣送出（含跨午夜與空段）", () => {
    const s = formStateFromConfig({ ...READBACK, schedule: GOOD_SCHEDULE } as never);
    assert.deepEqual(validateScriptLayer(s), []);
    assert.deepEqual(buildTransferCallConfig(s).schedule, GOOD_SCHEDULE);
    assert.ok(segmentWrapsMidnight(["22:00", "02:00"]));
    assert.ok(!segmentWrapsMidnight(["09:00", "18:00"]));
});

check("週表反例與正本同組：9:00／25:00／快照外 tz／未知日鍵／單元素段", () => {
    const cases: Array<[Record<string, unknown>, string]> = [
        [{ mon: [["9:00", "18:00"]] }, "schedule.mon[0]"],
        [{ mon: [["09:00", "25:00"]] }, "schedule.mon[0]"],
        [{ tz: "Asia/Nowhere" }, "schedule.tz"],
        [{ xyz: [["09:00", "18:00"]] }, "schedule.xyz"],
        [{ mon: [["09:00"]] }, "schedule.mon[0]"],
    ];
    for (const [sched, field] of cases) {
        const s = formStateFromConfig(READBACK as never);
        s.schedule = sched as never;
        const problems = validateScriptLayer(s);
        assert.ok(problems.some((p) => p.field === field), `${JSON.stringify(sched)} 應指名 ${field}，得 ${JSON.stringify(problems)}`);
    }
});

check("必填話術清空 → 指名欄位（鏡像閘門 required_keys）", () => {
    const s = formStateFromConfig(READBACK as never);
    s.transferFailedMessage = "   ";
    const fields = validateScriptLayer(s).map((p) => p.field);
    assert.deepEqual(fields, ["transferFailedMessage"]);
});

check("列舉與範圍：hangup／voice／0／11／3.5 皆拒；1–10 整數放行", () => {
    const base = formStateFromConfig(READBACK as never);
    assert.ok(validateScriptLayer({ ...base, afterHoursAction: "hangup" as never }).some((p) => p.field === "afterHoursAction"));
    assert.ok(validateScriptLayer({ ...base, messageType: "voice" as never }).some((p) => p.field === "messageType"));
    for (const v of [0, 11, 3.5]) {
        assert.ok(validateScriptLayer({ ...base, unavailableAnnounceLimit: v }).some((p) => p.field === "unavailableAnnounceLimit"), `${v}`);
    }
    for (const v of [1, 5, 10, null]) assert.deepEqual(validateScriptLayer({ ...base, unavailableAnnounceLimit: v }), []);
});

check("timeout 不驗（本部署不生效，唯讀），但仍帶現值送出", () => {
    const s = formStateFromConfig({ ...READBACK, timeout: 999 } as never);
    assert.deepEqual(validateScriptLayer(s), []);
    assert.equal(buildTransferCallConfig(s).timeout, 999);
});

check("反例：{...spread} 回讀會帶回六欄——本 builder 不採用那種寫法", () => {
    const spread = { ...READBACK, customMessage: "x" };
    assert.ok(CCP_TRANSFER_DEPLOYMENT_KEYS.every((k) => k in spread), "spread 形狀確實含六欄（這正是不採用的理由）");
    const body = buildTransferCallConfig(formStateFromConfig(spread as never));
    assert.ok(CCP_TRANSFER_DEPLOYMENT_KEYS.every((k) => !(k in body)));
});

console.log(`${passed} passed`);
