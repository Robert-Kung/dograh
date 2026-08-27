"use client";

import posthog from "posthog-js";
import { createContext, type ReactNode,useCallback, useContext, useMemo, useRef, useState } from "react";

import { EnterpriseModal } from "@/components/lead-forms/EnterpriseModal";
import { HireExpertModal } from "@/components/lead-forms/HireExpertModal";
import type { LeadSource } from "@/components/lead-forms/leadFieldOptions";
import { PostHogEvent } from "@/constants/posthog-events";

interface LeadFormsContextValue {
  openHireExpert: (source: LeadSource) => void;
  openEnterprise: (source: LeadSource, prefill?: { company?: string }) => void;
  // True once the hire modal has been opened this session (used to suppress the builder nudge).
  hasOpenedHireRef: React.MutableRefObject<boolean>;
}

const LeadFormsContext = createContext<LeadFormsContextValue | null>(null);

export function LeadFormsProvider({ children }: { children: ReactNode }) {
  const [hireOpen, setHireOpen] = useState(false);
  const [enterpriseOpen, setEnterpriseOpen] = useState(false);
  // Track the originating source so the *_OPENED and submit events agree.
  const [hireSource, setHireSource] = useState<LeadSource>("sidebar");
  const [enterpriseSource, setEnterpriseSource] = useState<LeadSource>("sidebar");
  const [enterprisePrefill, setEnterprisePrefill] = useState<{ company?: string } | undefined>(undefined);
  const hasOpenedHireRef = useRef(false);

  // ---- Post-signup onboarding gate（**本 fork 已移除**）----
  // 上游在這裡做的是：完成/略過旗標未設 **且** 使用者的 workflow 數為 0 時，
  // 自動彈出一次性的 onboarding 表單。
  // customer-center-platform fork（母 repo §6 review F-4）：`OnboardingModal`、
  // 自動開啟它的 effect（觸發鏈是 `getWorkflowCount() === 0`＝全新交付的第一次登入）、
  // 兩個守門 ref 與 `completeOnboarding` **一併移除，不留死碼**——留一個只會
  // `setOnboardingOpen` 而沒有消費者的 effect，下一輪 rebase 會有人把它接回去。
  // 移除理由見下方 Provider 內的註解。`useOnboarding()` 因此在本檔已無讀取點。

  const openHireExpert = useCallback((source: LeadSource) => {
    hasOpenedHireRef.current = true;
    setHireSource(source);
    setHireOpen(true);
    posthog.capture(PostHogEvent.HIRE_EXPERT_OPENED, { source });
  }, []);

  const openEnterprise = useCallback((source: LeadSource, prefill?: { company?: string }) => {
    setEnterpriseSource(source);
    setEnterprisePrefill(prefill);
    setEnterpriseOpen(true);
    posthog.capture(PostHogEvent.ENTERPRISE_LEAD_OPENED, { source });
  }, []);

  const value = useMemo(
    () => ({ openHireExpert, openEnterprise, hasOpenedHireRef }),
    [openHireExpert, openEnterprise],
  );

  return (
    <LeadFormsContext.Provider value={value}>
      {children}
      <HireExpertModal
        open={hireOpen}
        onOpenChange={setHireOpen}
        source={hireSource}
        onOpenEnterprise={() => openEnterprise("hire_expert")}
      />
      <EnterpriseModal
        open={enterpriseOpen}
        onOpenChange={setEnterpriseOpen}
        source={enterpriseSource}
        prefill={enterprisePrefill}
      />
      {/* customer-center-platform fork（母 repo §6 review F-4）：
          上游的 `OnboardingModal` 已移除，理由與 `HireExpertNudge`（task 3.4e）
          **完全同型**——同一支 `onboardingServiceClient`、同一個外部 host
          `api-leads.dograh.com`（無 auth、identity 就是使用者填的 email）、同一套
          把 4xx／5xx 與網路錯誤全吞進 `console.error` 的 best-effort；閘門的 CSP
          `connect-src 'self'` 會擋掉那個跨源 POST ⇒ **假成功**（AC3c 的類別）。

          它比那一顆更該移除的地方有兩點：
          ① 它**自動開啟且擋住畫面**（不是角落的 nudge）；
          ② 觸發條件是 `getWorkflowCount() === 0`，也就是**全新交付的第一次登入**
             ——客戶主管看到的第一個畫面會是一份英文的阻斷式表單。

          **今天它不會觸發，但那是巧合不是設計**：`GET /user/onboarding-state` 對
          兩個角色皆 deny ⇒ `OnboardingContext` 走「fetch 失敗就停在 loading」⇒
          上方那個 effect 的 `onboardingLoading` 永遠為 true ⇒ 永不執行。
          那條 fail-closed 沒有寫在任何一份正本裡，而它的守門條件恰好與
          R-X（主管新取得的讀取面）的收斂方向相反——哪天把 onboarding-state 開給
          主管，這份表單就會跳出來。移掉它，這條隱式相依就不必被記住。 */}
    </LeadFormsContext.Provider>
  );
}

export function useLeadForms(): LeadFormsContextValue {
  const ctx = useContext(LeadFormsContext);
  if (!ctx) throw new Error("useLeadForms must be used within a LeadFormsProvider");
  return ctx;
}
