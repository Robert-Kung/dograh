// customer-center-platform fork（母 repo W4a D7）：側欄隱藏清單與分區過濾。
//
// 跑法：`npm run test:ccp-ui-denied`（Node 24+），或
//   docker run --rm -v "$PWD:/ui" -w /ui node:24-alpine \
//     node scripts/ccp-ui-denied.test.mts
// 慣例同 `ccp-access-rules.test.mts`：零依賴、node 直接跑。
//
// 閘門副本與正本的對帳**不在這裡**：那是部署期的事，由母 repo preflight §7c2 執行。

import assert from "node:assert/strict";

import {
    CCP_UI_DENIED_NAMES,
    consoleOverviewUrl,
    filterSidebarSections,
    isHiddenSidebarUrl,
} from "../src/lib/ccp/ui-denied.ts";

let passed = 0;
function check(name: string, fn: () => void) {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
}

console.log("ccp ui-denied");

// 與 AppSidebar 的 NAV_SECTIONS 同形（標題、網址、分區）——側欄改版時兩邊一起改。
const UPSTREAM_SECTIONS = [
    { items: [{ title: "Overview", url: "/overview" }] },
    {
        label: "BUILD",
        items: [
            { title: "Voice Agents", url: "/workflow" },
            { title: "Campaigns", url: "/campaigns" },
            { title: "Models", url: "/model-configurations" },
            { title: "Telephony", url: "/telephony-configurations" },
            { title: "Tools", url: "/tools" },
            { title: "Files", url: "/files" },
            { title: "Recordings", url: "/recordings" },
            { title: "Developers", url: "/api-keys" },
        ],
    },
    {
        label: "MANAGE",
        items: [
            { title: "Agent Runs", url: "/usage" },
            { title: "Billing", url: "/billing" },
            { title: "Reports", url: "/reports" },
        ],
    },
];

check("側欄只剩 Voice Agents／Models／Tools", () => {
    const out = filterSidebarSections(UPSTREAM_SECTIONS);
    const titles = out.flatMap((s) => s.items.map((i) => i.title));
    assert.deepEqual(titles, ["Voice Agents", "Models", "Tools"]);
});

check("入口全數隱藏的分區不留下（無空的群組標題）", () => {
    const out = filterSidebarSections(UPSTREAM_SECTIONS);
    assert.deepEqual(out.map((s) => s.label), ["BUILD"]);
});

check("首段比對：子路徑同樣隱藏，相似前綴不誤殺", () => {
    assert.equal(isHiddenSidebarUrl("/campaigns/12"), true);
    assert.equal(isHiddenSidebarUrl("/overview"), true);
    assert.equal(isHiddenSidebarUrl("/workflow"), false);
    assert.equal(isHiddenSidebarUrl("/filesystem"), false);
    assert.equal(isHiddenSidebarUrl("/usage?x=1"), true);
});

check("閘門副本十三個名稱、無重複", () => {
    assert.equal(CCP_UI_DENIED_NAMES.length, 13);
    assert.equal(new Set(CCP_UI_DENIED_NAMES).size, 13);
});

check("回主控台：同主機名、標準埠、固定路徑", () => {
    assert.equal(consoleOverviewUrl("ops.example.com"), "https://ops.example.com/desk/overview");
});

console.log(`${passed} passed`);
