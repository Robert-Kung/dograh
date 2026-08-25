// customer-center-platform fork（母 repo W2d task 7.1 / AC2b）：
// `resolveAccess` 的四條 fail-safe 分支。
//
// 跑法：`npm run test:ccp-access`（自 ui/），或
// `node scripts/ccp-access-rules.test.mts`（Node 24+ 原生剝型別）。
// 本機／建置容器是 Node 18／20，兩者都不剝型別 ⇒ 用容器跑：
//   docker run --rm -v "$PWD:/ui" -w /ui node:24-alpine \
//     node scripts/ccp-access-rules.test.mts
//
// **為什麼是這個形狀而不是 vitest**：上游 ui 沒有任何 test runner，為了四個
// 分支引進一套等於在每次 rebase 都要處理的 fork 裡多養一組 devDependency，
// 而供應鏈面已經是登記在案的殘留（R-AD）。上游自己的
// `scripts/test-display-options.mts` 就是這個慣例——零依賴、node 直接跑。
// 照抄慣例的另一個好處：rebase 時它長得跟上游的東西一樣，不會被當成噪音刪掉。

import assert from "node:assert/strict";

import {
    localRole,
    resolveAccess,
} from "../src/lib/ccp/access-rules.ts";

let passed = 0;
function check(name: string, fn: () => void) {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
}

console.log("ccp access rules");

// ── 正向：兩個已知角色 ────────────────────────────────────────────────
check("實施方 ⇒ writable", () => {
    const a = resolveAccess({
        loading: false,
        isAuthenticated: true,
        user: { provider: "local", role: "implementer" },
    });
    assert.deepEqual(a, { state: "writable", readOnly: false, role: "implementer" });
});

check("主管 ⇒ readonly-role（不是 signal-unavailable）", () => {
    const a = resolveAccess({
        loading: false,
        isAuthenticated: true,
        user: { provider: "local", role: "supervisor" },
    });
    assert.deepEqual(a, { state: "readonly-role", readOnly: true, role: "supervisor" });
});

// ── fail-safe 四條（AC2b）：一律 readOnly，且 MUST NOT 是 readonly-role ──
//
// 兩種狀態分開的理由寫在 access.tsx：一個**實施方** session 在閘門設定錯誤下
// 會看到「運作正常的唯讀介面 ＋ 滿頁『請找實施方』」，而沒有任何一處說得出
// 「訊號取不到」。文案分支吃的就是 state。

check("① 還在載入 ⇒ loading 且唯讀（舊慣例正是在這一格開出可寫窗口）", () => {
    const a = resolveAccess({ loading: true, isAuthenticated: false, user: null });
    assert.deepEqual(a, { state: "loading", readOnly: true, role: null });
});

check("② 未認證／訊號請求失敗 ⇒ signal-unavailable", () => {
    const a = resolveAccess({ loading: false, isAuthenticated: false, user: null });
    assert.deepEqual(a, { state: "signal-unavailable", readOnly: true, role: null });
});

check("③ 已認證但回應無 role 欄 ⇒ signal-unavailable，不是可寫", () => {
    const a = resolveAccess({
        loading: false,
        isAuthenticated: true,
        user: { provider: "local" },
    });
    assert.deepEqual(a, { state: "signal-unavailable", readOnly: true, role: null });
});

check("④ 未知角色值（閘門日後新增而前端沒同步）⇒ signal-unavailable", () => {
    const a = resolveAccess({
        loading: false,
        isAuthenticated: true,
        user: { provider: "local", role: "auditor" },
    });
    assert.deepEqual(a, { state: "signal-unavailable", readOnly: true, role: null });
});

// ── 邊界：role 只認 local provider，且必須是字串 ──────────────────────

check("非 local provider 的 role 不採信", () => {
    assert.equal(localRole({ provider: "stack", role: "implementer" }), null);
    const a = resolveAccess({
        loading: false,
        isAuthenticated: true,
        user: { provider: "stack", role: "implementer" },
    });
    assert.equal(a.readOnly, true);
});

check("role 是非字串（物件／布林）不採信——不得因 truthy 而過", () => {
    assert.equal(localRole({ provider: "local", role: { $ne: null } }), null);
    assert.equal(localRole({ provider: "local", role: true }), null);
});

check("空字串 role ⇒ signal-unavailable（`!role` 這一格）", () => {
    const a = resolveAccess({
        loading: false,
        isAuthenticated: true,
        user: { provider: "local", role: "" },
    });
    assert.equal(a.state, "signal-unavailable");
});

// ── 反向護欄：唯一 readOnly=false 的出口 ─────────────────────────────
//
// 這條在於「日後有人加一個新的可寫狀態」時會紅。readOnly 是全部呈現面的
// 單一消費點，多一個 false 出口就是多一批按得下去的按鈕。

check("除了 writable，沒有任何輸入組合得到 readOnly=false", () => {
    const inputs: Array<{ loading: boolean; isAuthenticated: boolean; user: unknown }> = [
        { loading: true, isAuthenticated: true, user: { provider: "local", role: "implementer" } },
        { loading: false, isAuthenticated: false, user: { provider: "local", role: "implementer" } },
        { loading: false, isAuthenticated: true, user: null },
        { loading: false, isAuthenticated: true, user: {} },
        { loading: false, isAuthenticated: true, user: { provider: "local", role: "supervisor" } },
        { loading: false, isAuthenticated: true, user: { provider: "local", role: "SUPERVISOR" } },
        { loading: false, isAuthenticated: true, user: { provider: "local", role: "Implementer" } },
    ];
    for (const args of inputs) {
        const a = resolveAccess(args);
        assert.equal(a.readOnly, true, JSON.stringify(args));
        assert.notEqual(a.state, "writable", JSON.stringify(args));
    }
});

check("大小寫變體不得成為可寫（角色比對不做正規化，列舉即閉集）", () => {
    for (const role of ["Implementer", "IMPLEMENTER", " implementer"]) {
        const a = resolveAccess({
            loading: false,
            isAuthenticated: true,
            user: { provider: "local", role },
        });
        assert.equal(a.state, "signal-unavailable", role);
    }
});

console.log(`\n${passed} passed`);
