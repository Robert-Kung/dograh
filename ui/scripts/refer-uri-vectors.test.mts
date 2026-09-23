// customer-center-platform fork（母 repo W3b task 4.2）：
// `lib/ccp/refer-uri.ts` 對正本 `deploy/bin/testdata/uri/vectors.json` 的**全量**回歸。
//
// 跑法：`npm run test:refer-uri`（自 ui/；Node 24+ 原生剝型別），或
//   docker run --rm -v "$PWD:/ui" -v "<repo>/deploy/bin/testdata/uri:/vectors:ro" -w /ui \
//     -e CCP_URI_VECTORS=/vectors/vectors.json node:24-alpine node scripts/refer-uri-vectors.test.mts
//
// **57 條全跑、MUST NOT 篩選**（設計 D6）：只跑「看起來相關」的子集會讓複本悄悄
// 比正本寬。比 `expect.ok` 與 `expect.premium_candidates`；理由碼刻意不對齊。
// 向量檔缺席＝**失敗**，不是跳過——這支存在的理由就是那份檔。

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";

import { parseReferUri } from "../src/lib/ccp/refer-uri.ts";

const vectorsPath =
    process.env.CCP_URI_VECTORS ?? path.resolve(process.cwd(), "../../deploy/bin/testdata/uri/vectors.json");

let raw: string;
try {
    raw = readFileSync(vectorsPath, "utf-8");
} catch {
    console.error(`FAIL: 找不到向量檔 ${vectorsPath}（以 CCP_URI_VECTORS 指定路徑；缺檔不是跳過）`);
    process.exit(1);
}
const vectors: Array<{ value: string; why: string; expect: { ok: boolean; premium_candidates?: string[] } }> =
    JSON.parse(raw).vectors;

const EXPECTED_COUNT = 57;
assert.equal(vectors.length, EXPECTED_COUNT, `向量數應為 ${EXPECTED_COUNT}（正本改了就同批改這裡）`);

let passed = 0;
const failures: string[] = [];
for (const v of vectors) {
    const got = parseReferUri(v.value);
    const problems: string[] = [];
    if (got.ok !== v.expect.ok) problems.push(`ok: got ${got.ok}, want ${v.expect.ok}`);
    if (v.expect.ok) {
        const want = v.expect.premium_candidates ?? [];
        if (JSON.stringify([...got.premiumCandidates]) !== JSON.stringify(want)) {
            problems.push(`premium_candidates: got ${JSON.stringify(got.premiumCandidates)}, want ${JSON.stringify(want)}`);
        }
    }
    if (problems.length) {
        failures.push(`  FAIL ${JSON.stringify(v.value)} — ${problems.join("; ")}（${v.why}）`);
    } else {
        passed += 1;
    }
}

console.log(`refer-uri vectors: ${passed}/${vectors.length} ok`);
if (failures.length) {
    console.log(failures.join("\n"));
    process.exit(1);
}
