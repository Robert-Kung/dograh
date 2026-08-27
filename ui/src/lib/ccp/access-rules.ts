/**
 * customer-center-platform fork：唯讀訊號的**純判定**（母 repo W2d task 2.1b／7.1）。
 *
 * **為什麼自 `access.tsx` 拆出來**：`resolveAccess` 的四條 fail-safe 分支是本 change
 * 的承重規則（AC2b），而它原本住在一個 `import React` 的 `.tsx` 裡 ⇒ 想測它就得先
 * 有一整套 React 測試環境。上游 dograh 的 ui **沒有任何 test runner**（`package.json`
 * 的 scripts 內無 vitest／jest），為了測四個分支而引進一套，等於在一個每次 rebase
 * 都要處理的 fork 裡多養一組 devDependency，而供應鏈面已經是登記在案的殘留（R-AD）。
 *
 * 上游自己有一個現成的慣例：`scripts/test-display-options.mts` ——一支 `.mts`、
 * 用 `node` 原生的 TS type stripping 跑、零依賴。本檔拆成**不 import React**
 * 的純模組，就能照同一個慣例測（`scripts/ccp-access-rules.test.mts`）。
 *
 * `access.tsx` 原樣 re-export 這裡的每一個名字，呼叫端不需要改 import。
 */

/** 閘門 `allowlist.py` 的 `KNOWN_ROLES`。這裡刻意列舉而非接受任何字串——
 *  未知角色值（含閘門日後新增而前端沒同步的）落 `signal-unavailable`，不是可寫。 */
export const CCP_KNOWN_ROLES = ['supervisor', 'implementer'] as const;
export type CcpRole = (typeof CCP_KNOWN_ROLES)[number];

/** 可寫的角色。目前只有實施方；主管是唯讀角色。 */
export const WRITABLE_ROLES: readonly string[] = ['implementer'];

export type CcpAccessState =
    /** 還在取訊號。**視為唯讀**——這正是舊慣例開出可寫窗口的那一格。 */
    | 'loading'
    /** 已確認是可寫角色（實施方）。 */
    | 'writable'
    /** 已確認是唯讀角色（主管）。文案講「這個平台的話術由實施方維護」。 */
    | 'readonly-role'
    /** 訊號不可得（未認證／fetch 失敗／`role` 缺失／未知角色值）。
     *  文案 SHALL 講「取不到權限訊號」，MUST NOT 講成「你是唯讀角色」。 */
    | 'signal-unavailable';

export interface CcpAccess {
    state: CcpAccessState;
    /** 唯一的消費點：只有 `writable` 是 false。 */
    readOnly: boolean;
    /** 已確認的角色；`loading`／`signal-unavailable` 時為 null。 */
    role: CcpRole | null;
}

export const CCP_ACCESS_FALLBACK: CcpAccess = {
    state: 'signal-unavailable',
    readOnly: true,
    role: null,
};

/**
 * 只認 `provider === 'local'` 的 `role`。
 *
 * `AuthUser = CurrentUser | LocalUser`，而 `CurrentUser` 自 `@stackframe/stack`
 * import ⇒ 我方擴不了它。故 `role` 補在 `BaseUser`／`LocalUser` 上，讀取點以
 * provider 窄化到 `LocalUser`（W2d task 2.3）。`as any` 會讓型別擋不住任何東西，
 * 而「TS 會擋」正是那條 task 存在的理由。
 *
 * 參數型別在這裡刻意寫成 `unknown` 而不是 import `AuthUser`：那個型別鏈會把
 * `@stackframe/stack` 拉進來，本檔就不再是零依賴、也就測不動了。
 *
 * **但「TS 會擋」那條紀律沒有因此消失**（§6 review M-8）：`access.tsx` 有一層
 * 薄包裝把參數釘回 `AuthUser | null`，型別護欄留在 fork 邊界上。上游 rebase
 * 改了 `LocalUser` 的欄位名時，紅的是那一層——而不是靜默讓所有人落
 * `signal-unavailable`（fail-closed 沒錯，但那是 build 期發現不了的功能全失）。
 */
export function localRole(user: unknown): string | null {
    if (!user) return null;
    const provider = (user as { provider?: unknown }).provider;
    if (provider !== 'local') return null;
    const role = (user as { role?: unknown }).role;
    return typeof role === 'string' ? role : null;
}

export function resolveAccess(args: {
    loading: boolean;
    isAuthenticated: boolean;
    user: unknown;
}): CcpAccess {
    if (args.loading) return { state: 'loading', readOnly: true, role: null };
    if (!args.isAuthenticated) return CCP_ACCESS_FALLBACK;

    const role = localRole(args.user);
    if (!role) return CCP_ACCESS_FALLBACK;
    if (!(CCP_KNOWN_ROLES as readonly string[]).includes(role)) return CCP_ACCESS_FALLBACK;

    const known = role as CcpRole;
    return WRITABLE_ROLES.includes(known)
        ? { state: 'writable', readOnly: false, role: known }
        : { state: 'readonly-role', readOnly: true, role: known };
}
