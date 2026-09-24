/**
 * customer-center-platform fork（母 repo W4a D7）：編輯器側欄只呈現本部署可達的入口。
 *
 * 兩份清單，性質不同：
 *
 * 1. `CCP_UI_DENIED_NAMES`——**閘門 `_UI_DENIED_NAMES` 的副本**
 *    （`services/editor-gateway/app/main.py`）。閘門是 UI 面拒絕的正本與執行點；
 *    這份副本只決定側欄畫不畫那個入口。依 U-22 慣例「硬編副本＋部署期對帳」：
 *    母 repo 的 preflight §7c 以 AST 讀閘門原文、以 regex 讀本檔，集合不等即阻擋
 *    部署並指出差異。**改這份清單就要同批改閘門，反之亦然**——否則部署被擋。
 *    本檔的字面格式是對帳腳本的解析面：每個名稱一個單引號字串、陣列以 `] as const`
 *    結束，不要改成拼接或展開。
 *
 * 2. `CCP_SIDEBAR_EXTRA_HIDDEN`——閘門**放行**、但屬上游行銷面的入口，每項附理由。
 *    不參與對帳（閘門沒有對應物）。
 *
 * 本檔零依賴，供 `scripts/ccp-ui-denied.test.mts` 以 node 直接載入。
 */

export const CCP_UI_DENIED_NAMES = [
    'monitoring',
    'ingest',
    'superadmin',
    'recordings',
    'api-keys',
    'impersonate',
    'reports',
    'campaigns',
    'billing',
    'usage',
    'automation',
    'files',
    'telephony-configurations',
] as const;

export const CCP_SIDEBAR_EXTRA_HIDDEN: ReadonlyArray<{ url: string; why: string }> = [
    { url: '/overview', why: '上游行銷頁（GitHub star、社群與文件外連），交付態無對應內容' },
];

function firstSegment(url: string): string {
    return url.replace(/^\/+/, '').split(/[/?#]/, 1)[0] ?? '';
}

const DENIED = new Set<string>(CCP_UI_DENIED_NAMES);
const EXTRA = new Set<string>(CCP_SIDEBAR_EXTRA_HIDDEN.map((e) => firstSegment(e.url)));

/** 首段比對：`/campaigns`、`/campaigns/12` 皆隱藏；`/workflow` 不受影響。 */
export function isHiddenSidebarUrl(url: string): boolean {
    const seg = firstSegment(url);
    return DENIED.has(seg) || EXTRA.has(seg);
}

/** 過濾側欄分區；入口全數隱藏的分區整個拿掉（不留空的群組標題）。 */
export function filterSidebarSections<S extends { items: ReadonlyArray<{ url: string }> }>(
    sections: ReadonlyArray<S>,
): S[] {
    return sections
        .map((s) => ({ ...s, items: s.items.filter((i) => !isHiddenSidebarUrl(i.url)) }))
        .filter((s) => s.items.length > 0);
}

/**
 * 「回主控台」的目的地：console 與編輯器同主機名、console 在標準 HTTPS 埠
 * （母 repo 殘留風險登記此假設）。不帶任何憑證。
 */
export function consoleOverviewUrl(hostname: string): string {
    return `https://${hostname}/desk/overview`;
}

/** 與 console 端 `window.name` 相同，使連結回到已開著的 console 分頁。 */
export const CCP_CONSOLE_WINDOW = 'ccp-console';
