import type { CurrentUser } from '@stackframe/stack';

// Base user interface that all providers must support
export interface BaseUser {
  id: string;
  email?: string;
  name?: string;
  image?: string;
  /**
   * customer-center-platform fork（母 repo W2d task 2.3）。
   *
   * 由編輯器閘門自產的 `/api/auth/oss` 帶回（`_oss_auth_identity`）。上游沒有這個
   * 欄位；`setUser(data.user)` 因為型別寬鬆不會擋，但任何 `user.role` 讀取點會 TS
   * 報錯，而最省事的修法是 `as any`——那會讓型別擋不住任何東西。
   *
   * 宣告在 `BaseUser` 上，`LocalUser` 由繼承取得（`AuthUser` 的另一半 `CurrentUser`
   * 自 `@stackframe/stack` import，我方擴不了，故讀取點一律以 `provider === 'local'`
   * 窄化——見 `@/lib/ccp/access` 的 `localRole()`）。
   *
   * **這不是授權欄位**：它走瀏覽器可見、可改寫的路徑，只用來畫介面。授權在閘門的
   * `decide()`。
   */
  role?: string;
}

// Local/OSS user type
export interface LocalUser extends BaseUser {
  provider: 'local';
  organizationId?: string;
  displayName?: string;
  provider_id?: string;
}

// Union type for all user types
export type AuthUser = CurrentUser | LocalUser;


export interface AuthToken {
  accessToken: string;
  refreshToken?: string;
  expiresAt?: number;
}

export interface TeamPermission {
  id: string;
}

export type AuthProvider = 'stack' | 'local';

export interface AuthConfig {
  provider: AuthProvider;
  // Provider-specific configuration
  [key: string]: string | number | boolean;
}

