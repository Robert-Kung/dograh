/**
 * customer-center-platform fork（母 repo W3b task 4.1，關閉 `ccp#1`）：
 * REFER 目的地（`tel:`／`sip:`）形狀判準的 **JS 複本**。
 *
 * 正本是母 repo `deploy/bin/sip_uri.py`（stdlib-only Python）。三個 Python 執行點
 * 共用那一份；瀏覽器沒有共用載體，故本檔是**逐規則移植**（與 `credential_key_names`
 * 同型的刻意複本），守衛是 `scripts/refer-uri-vectors.test.mts`——正本的
 * `testdata/uri/vectors.json` **57 條全跑、不篩選**，比 `ok` 與 `premium_candidates`。
 *
 * ## 為什麼要移植而不是「取聯集」
 *
 * W2a 之前 UI 收 `+E164`／`PJSIP/…`（Asterisk ARI 方言），後端自 D-A6 起只收
 * `tel:`／`sip:`——**兩者交集為空**：UI 判合法的每一個值送出都 403。`ccp#1` 原本的
 * 修法「取聯集」已因 D-A6 過時：聯集會讓 UI 放行執行層撥不出去的形狀（啞彈）。
 * 核心不變量與正本相同：**本檔接受的集合 SHALL 是正本接受集合的子集，MUST NOT 更寬。**
 *
 * ## 部署期零執行點
 *
 * 本檔只決定畫面上的即時提示；控制面是閘門的 `constrained_values.destination`
 * （`refer_uri` kind，同一份 `sip_uri.py`）。交付態下 destination 欄位根本不畫
 * （部署層六欄，`CCP_TRANSFER_DEPLOYMENT_KEYS`），本檔只在 fork 的非 CCP 路徑生效；
 * 仍做，因為 `ccp#1` 是為 fork 的其他使用者而開。漂移登記於母 repo RESIDUAL-RISKS。
 *
 * ## 理由碼**不**對齊
 *
 * `reason` 是給畫面的繁中固定文案；JS 側不需要與 `sip_uri.REASONS` 同名。向量測試
 * 只比 `ok` 與 `premiumCandidates`（設計 D6）。**文案不插入原值**（正本的輸出紀律——
 * 這裡的值是使用者剛打的，回顯無害，但保持同一條紀律免得複本被拿去別處用）。
 */

export interface ReferUriResult {
    ok: boolean;
    /** ok=false 時的固定文案（繁中）；ok=true 時為空字串。 */
    reason: string;
    /** 高費率前綴比對的候選（去前導 `+`）：tel 只有號碼；sip 為 user 與 host。 */
    premiumCandidates: readonly string[];
    /** 規範化字串（sip 補預設 port）；ok=false 時為空字串。 */
    normalized: string;
}

/** 與正本 `sip_uri.SCHEME_DEFAULT_PORTS` 同形。`sips` 依子集不變量不收。 */
const SCHEME_DEFAULT_PORTS: Readonly<Record<string, number>> = { sip: 5060 };

// 正本 `_USER_RE`：只收 `[A-Za-z0-9._-]`（執行層字集的子集；`@` 是結構規則）。
const USER_RE = /^[A-Za-z0-9._-]+$/;
// 正本 `_HOST_LABEL_RE`：小寫化後比對，label 不得為空、不得以 `-` 起訖。
const HOST_LABEL_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
// 正本 `_TEL_RE`：E.164，`+` 起首、首位非 0、共 2–15 位。
const TEL_RE = /^\+[1-9][0-9]{1,14}$/;
// 正本 `_ARI_RE`／`_BARE_NUMBER_RE`：只為了給專屬理由。
const ARI_RE = /^(PJSIP|SIP)\//i;
const BARE_NUMBER_RE = /^\+?[0-9][0-9 ()-]*$/;

const REASONS = {
    empty: '請輸入轉接目的地',
    whitespace: '目的地不得含空白或控制字元（含前後空白與換行）',
    non_ascii: '目的地只能是 ASCII 字元（不支援 IDN／全形）',
    percent_encoding: '目的地不得含百分比編碼（%）',
    ari_dialect: 'SIP/… 與 PJSIP/… 是 Asterisk 方言，本部署撥不出去：請改為 tel:+E164 或 sip:user@host',
    bare_number: '裸號碼沒有 scheme：請改為 tel:+E164 或 sip:user@host',
    no_scheme: '目的地缺少 scheme（須為 tel: 或 sip:）',
    scheme_case: 'scheme 必須是小寫的 tel: 或 sip:',
    unsupported_scheme: '只接受 tel: 與 sip:（不支援 sips:）',
    sip_params: 'sip: 目的地不得含參數、標頭或路徑／片段（; ? / #）',
    sip_missing_at: 'sip: 目的地須為 user@host 形狀',
    sip_multiple_at: 'sip: 目的地只能有一個 @',
    sip_empty_user: 'sip: 目的地的 user 不得為空',
    sip_user_charset: 'sip: 目的地的 user 只能含英數字與 . _ -（不得含 :）',
    sip_empty_host: 'sip: 目的地的 host 不得為空',
    host_brackets: '不支援 IPv6 方括號形式的 host',
    host_trailing_dot: 'host 不得以點結尾',
    host_charset: 'host 不是合法的主機名形狀',
    host_port: 'port 不合法',
    tel_empty: 'tel: 目的地沒有號碼',
    tel_shape: 'tel: 目的地須為 E.164（+ 起首、首位非 0、共 2–15 位數字，不得含分隔符）',
} as const;

type ReasonCode = keyof typeof REASONS;

function reject(code: ReasonCode): ReferUriResult {
    return { ok: false, reason: REASONS[code], premiumCandidates: [], normalized: '' };
}

/**
 * 正本 `_parse_host`：走 `urlsplit('//' + hostpart)` 的語意——hostname 在**第一個**
 * `:` 之前並小寫化；port 須為純數字、0–65535（Python 3.12 `urlsplit().port` 的判準），
 * 再依正本拒 `port <= 0`。
 */
function parseHost(hostpart: string, scheme: string): { hostname: string; port: number } | ReferUriResult {
    if (!hostpart) return reject('sip_empty_host');
    if (hostpart.includes('[') || hostpart.includes(']')) return reject('host_brackets');
    // `sip:u@h:`——urlsplit 對空 port 回 None，會被預設 port 補起來而靜默通過。
    if (hostpart.endsWith(':')) return reject('host_port');

    const colon = hostpart.indexOf(':');
    const rawHost = colon === -1 ? hostpart : hostpart.slice(0, colon);
    const rawPort = colon === -1 ? null : hostpart.slice(colon + 1);
    const hostname = rawHost.toLowerCase();
    if (!hostname) return reject('sip_empty_host');
    // 非 ASCII 已在最外層拒；此處保留與正本同序的判準。
    if (/[^\x00-\x7f]/.test(hostname)) return reject('non_ascii');
    if (hostname.endsWith('.')) return reject('host_trailing_dot');
    if (hostname.split('.').some((label) => !HOST_LABEL_RE.test(label))) return reject('host_charset');

    let port: number;
    if (rawPort === null) {
        port = SCHEME_DEFAULT_PORTS[scheme];
    } else {
        if (!/^[0-9]+$/.test(rawPort)) return reject('host_port');
        port = Number.parseInt(rawPort, 10);
        if (port > 65535) return reject('host_port');
    }
    if (port <= 0) return reject('host_port');
    return { hostname, port };
}

function parseTel(rest: string): ReferUriResult {
    if (!rest) return reject('tel_empty');
    if (!TEL_RE.test(rest)) return reject('tel_shape');
    return {
        ok: true,
        reason: '',
        premiumCandidates: [rest.replace(/^\+/, '')],
        normalized: `tel:${rest}`,
    };
}

function parseSip(scheme: string, rest: string): ReferUriResult {
    if (/[;?/#]/.test(rest)) return reject('sip_params');
    const atCount = rest.split('@').length - 1;
    if (atCount === 0) return reject('sip_missing_at');
    if (atCount > 1) return reject('sip_multiple_at');

    const at = rest.indexOf('@');
    const user = rest.slice(0, at);
    const hostpart = rest.slice(at + 1);
    if (!user) return reject('sip_empty_user');
    if (!USER_RE.test(user)) return reject('sip_user_charset');

    const host = parseHost(hostpart, scheme);
    if ('ok' in host) return host;
    return {
        ok: true,
        reason: '',
        premiumCandidates: [user.replace(/^\+/, ''), host.hostname],
        normalized: `${scheme}:${user}@${host.hostname}:${host.port}`,
    };
}

/** 解析一個 REFER 目的地。不丟例外；`ok=false` 時看 `reason`。 */
export function parseReferUri(value: string): ReferUriResult {
    if (typeof value !== 'string') return reject('empty');
    if (!value) return reject('empty');
    // 不 trim：正本把任何空白／控制字元一律判拒（`tel:+886…\n` 曾在 `$` 收尾下假綠）。
    for (const ch of value) {
        const code = ch.codePointAt(0) ?? 0;
        if (/\s/.test(ch) || code < 0x20 || code === 0x7f) return reject('whitespace');
    }
    if (/[^\x00-\x7f]/.test(value)) return reject('non_ascii');
    if (value.includes('%')) return reject('percent_encoding');
    if (ARI_RE.test(value)) return reject('ari_dialect');

    if (!value.includes(':')) {
        return reject(BARE_NUMBER_RE.test(value) ? 'bare_number' : 'no_scheme');
    }
    const colon = value.indexOf(':');
    const scheme = value.slice(0, colon);
    const rest = value.slice(colon + 1);
    if (!scheme) return reject('no_scheme');
    const lowered = scheme.toLowerCase();
    if (!(lowered in SCHEME_DEFAULT_PORTS) && lowered !== 'tel') return reject('unsupported_scheme');
    if (scheme !== lowered) return reject('scheme_case');
    return scheme === 'tel' ? parseTel(rest) : parseSip(scheme, rest);
}

/**
 * 高費率號段前綴（母 repo `feature_scope_check.PREMIUM_RATE_PREFIXES` 的複本，
 * 三份同值、各有讀者——R-A）。**只提示不擋**：目的地清單仍不存在（R-E／CS-19）。
 */
export const CCP_PREMIUM_RATE_PREFIXES = ['1900', '1976', '886204'] as const;

export function isPremiumRateCandidate(result: ReferUriResult): boolean {
    return result.ok && result.premiumCandidates.some((c) => CCP_PREMIUM_RATE_PREFIXES.some((p) => c.startsWith(p)));
}
