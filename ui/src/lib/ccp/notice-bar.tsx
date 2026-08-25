'use client';

/**
 * customer-center-platform fork：頁面級說明條與停用態的可及性契約
 * （母 repo W2d task 3.2／3.2b／3.2c）。
 *
 * 這個檔**不是**上游 dograh 的一部分（fork-only，W2d task 2.0c）。
 *
 * ## 3.2 —— 載體是「頁面級說明條」，不是逐顆按鈕的 tooltip
 *
 * tooltip 在觸控裝置上不可達，而且它要回答的是**流程**問題（誰可以改、走什麼
 * 流程），不是欄位問題。故說明掛在頁面頂部、與角色綁定、持久可見。
 *
 * ## 3.2b —— 停用 vs 隱藏的選擇規則（**寫死在這裡**）
 *
 * **預設停用 ＋ 說明。** 只有「入口本身通往一個不可達的頁面」才移除
 * （現況兩顆合格：`DocumentSelector` 的 `Upload Documents` → `/files`、
 * `MCPSection` 的 `Get your API key` → `/api-keys`，兩者都在閘門的
 * `_UI_DENIED_NAMES` 內）。採移除者 SHALL 在該處註明理由。
 *
 * 理由：Header 的 Publish／Phone Call／Save 今天是**條件渲染**，照同一個模式
 * 接上 readOnly 的話主管的按鈕會整個消失——那既違反「停用 ＋ 說明」，也讓
 * 對照表的「按鈕啟用態」欄分不出「已處置地隱藏」與「本來就沒畫」。
 *
 * ## 3.2c —— 停用態的可及性契約（三選一，**選 `aria-disabled`**）
 *
 * 原生 `disabled` 的按鈕不進 tab 序，鍵盤與螢幕閱讀器使用者**遇不到那顆按鈕**，
 * 也就遇不到「為什麼不能按」——把 tooltip 否掉之後，這條會讓整個說明面對他們
 * 等於不存在。故停用一律經 `ccpDisabledProps()`：按鈕仍可聚焦、仍會被讀出，
 * 以 `aria-disabled` 宣告狀態、以 `aria-describedby` 指到本頁的說明條，
 * 並在 click 端擋掉實際動作。
 */

import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';

import { cn } from '@/lib/utils';

import { useCcpAccess } from './access';

/** 說明條的固定 id：`ccpDisabledProps()` 的 `aria-describedby` 預設指向它。 */
export const CCP_ACCESS_NOTICE_ID = 'ccp-access-notice';

export interface CcpNoticeCopy {
    title: string;
    message: string;
}

/**
 * 預設文案。**`readonlyRole` 與 `signalUnavailable` MUST NOT 共用**
 * （W2d task 2.1b／D-D9）：訊號壞掉時看到畫面的可能正是**可寫**身分，
 * 對他說「您是唯讀使用者」的話，畫面上沒有任何一處說得出真正的原因。
 */
export const CCP_NOTICE_COPY = {
    readonlyRole: {
        title: '這個頁面對您是唯讀的',
        message:
            '您可以檢視與測試這裡的設定，但變更由負責建置的實施方進行。'
            + '需要調整話術或設定時，請與您的專案窗口提出，由實施方套用後即可在這裡看到。',
    },
    signalUnavailable: {
        title: '取不到權限訊號，暫時以唯讀呈現',
        message:
            '這不代表您沒有變更權限——是這個頁面問不到您的身分，所以先一律以唯讀呈現，'
            + '避免您改了半天卻存不下來。請重新載入頁面；若持續如此，請通知負責建置的窗口檢查編輯器閘門。',
    },
    loading: {
        title: '正在確認權限…',
        message: '確認完成前，這個頁面上的變更操作暫時停用。',
    },
} as const satisfies Record<string, CcpNoticeCopy>;

/**
 * 說明條只有**一個**（掛在 `AppLayout`），逐頁的專屬文案經這個插槽送進去。
 *
 * 為什麼不是「每頁自己畫一條」：頁面各畫各的話，① 主管會在同一頁看到兩條說明
 * （全域那條 ＋ 頁面那條），② DOM 順序要靠每個呼叫端自己記得放最前面，
 * ③ `aria-describedby` 指向的 id 會有兩份。插槽讓「一頁一條、且在最前面」
 * 變成結構保證而不是紀律。
 */
export interface CcpPageNoticeCopy {
    readonly supervisor?: CcpNoticeCopy;
    readonly implementer?: CcpNoticeCopy;
}

const CcpNoticeSlotContext = createContext<{
    copy: CcpPageNoticeCopy;
    set: (copy: CcpPageNoticeCopy | null) => void;
}>({ copy: {}, set: () => { } });

export function CcpNoticeSlotProvider({ children }: { children: React.ReactNode }) {
    const [copy, setCopy] = useState<CcpPageNoticeCopy>({});
    const value = useMemo(
        () => ({ copy, set: (next: CcpPageNoticeCopy | null) => setCopy(next ?? {}) }),
        [copy],
    );
    return (
        <CcpNoticeSlotContext.Provider value={value}>
            {children}
        </CcpNoticeSlotContext.Provider>
    );
}

/**
 * 頁面把自己的專屬文案掛進說明條。卸載時自動還原成預設文案——換頁後仍留著
 * 上一頁的說明，就是另一種「說了錯的原因」。
 *
 * `copy` 每次 render 都是新物件，故以其**內容**為相依項（頁面文案是常數字串，
 * 序列化成本可忽略）。
 */
export function useCcpPageNotice(copy: CcpPageNoticeCopy) {
    const { set } = useContext(CcpNoticeSlotContext);
    const key = JSON.stringify(copy);
    useEffect(() => {
        set(JSON.parse(key) as CcpPageNoticeCopy);
        return () => set(null);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [key]);
}

/** 給 server component 的頁面用：渲染它等於呼叫 `useCcpPageNotice`。 */
export function CcpPageNotice(copy: CcpPageNoticeCopy) {
    useCcpPageNotice(copy);
    return null;
}

interface CcpAccessNoticeProps {
    /** 已確認為唯讀角色（主管）時的文案。省略即用預設。 */
    readonly supervisor?: CcpNoticeCopy;
    /**
     * 可寫角色（實施方）也需要說明時給——**只有兩角色皆被拒的頁面需要**
     * （例如設定分屬部署層的工具頁）。省略則實施方看不到說明條。
     */
    readonly implementer?: CcpNoticeCopy;
    readonly className?: string;
}

/**
 * 頁面級說明條。**放在頁面內容的最前面**：3.2c 的契約要求說明在 DOM 順序上
 * 先於受影響的控制項（`aria-describedby` 之外的第二道保險，也讓
 * `role="status"` 的朗讀順序是對的）。
 */
export function CcpAccessNotice({
    supervisor,
    implementer,
    className,
}: CcpAccessNoticeProps) {
    const access = useCcpAccess();
    const slot = useContext(CcpNoticeSlotContext).copy;
    // 頁面掛進來的文案優先於 props（props 是掛載點給的預設）。
    const supervisorCopy = slot.supervisor ?? supervisor;
    const implementerCopy = slot.implementer ?? implementer;

    const copy: CcpNoticeCopy | null = (() => {
        switch (access.state) {
            case 'loading':
                return CCP_NOTICE_COPY.loading;
            case 'signal-unavailable':
                // 訊號不可得的文案**不可被覆寫**：頁面自訂的角色說明在這一格
                // 一定是錯的（我們並不知道使用者是誰）。
                return CCP_NOTICE_COPY.signalUnavailable;
            case 'readonly-role':
                return supervisorCopy ?? CCP_NOTICE_COPY.readonlyRole;
            case 'writable':
                return implementerCopy ?? null;
        }
    })();

    if (!copy) return null;

    return (
        <div
            id={CCP_ACCESS_NOTICE_ID}
            role="status"
            className={cn(
                'border-b border-amber-300 bg-amber-50 px-4 py-3 text-amber-950',
                'dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100',
                className,
            )}
        >
            <p className="text-sm font-semibold">{copy.title}</p>
            <p className="text-sm">{copy.message}</p>
        </div>
    );
}

/**
 * 停用一顆入口。**用這支，不要用原生 `disabled`**（3.2c）。
 *
 * 用法——spread **放在既有 props 之後**，停用時它的 `onClick` 才蓋得掉原本的：
 *
 * ```tsx
 * <Button onClick={save} {...ccpDisabledProps(isReadOnly)}>儲存</Button>
 * ```
 *
 * 未停用時回空物件，對可寫身分零影響。
 */
export interface CcpDisabledProps {
    'aria-disabled'?: true;
    'aria-describedby'?: string;
    'data-ccp-disabled'?: 'true';
    onClick?: (event: React.MouseEvent<HTMLElement>) => void;
}

/**
 * 回傳型別**只列這四個鍵**，不是 `ButtonHTMLAttributes`：後者帶著
 * `onSelect: ReactEventHandler`，與 Radix 的 `DropdownMenuItem`
 * （`onSelect: (event: Event) => void`）衝突，spread 到選單項目上會編不過。
 */
export function ccpDisabledProps(
    disabled: boolean,
    options?: { describedBy?: string },
): CcpDisabledProps {
    if (!disabled) return {};
    return {
        'aria-disabled': true,
        'aria-describedby': options?.describedBy ?? CCP_ACCESS_NOTICE_ID,
        'data-ccp-disabled': 'true',
        onClick: (event: React.MouseEvent<HTMLElement>) => {
            // 鍵盤的 Enter／Space 在 button 上同樣是 click，這一條一併擋住。
            event.preventDefault();
            event.stopPropagation();
        },
    };
}

export interface CcpReadOnlyFieldProps {
    readOnly?: true;
    'aria-readonly'?: true;
    'aria-describedby'?: string;
    'data-ccp-readonly'?: 'true';
}

/**
 * 唯讀頁的**輸入欄位**（§3 巡檢 F5，2026-08-25 拍板取 `readOnly`）。
 *
 * 巡檢實測：`/settings` 的五個 input 與 Models 頁的 Provider／Model／Base Url／
 * API Key 全部 `disabled=false, readOnly=false`，只有 Save 停用。使用者可以改
 * 一輪、以為改好了，再發現存不了——與「按下去才失敗」同族，只是位置從按鈕
 * 搬到了欄位。
 *
 * **為什麼是 `readOnly` 而不是 `disabled`**：3.3 刻意選了「唯讀但保留呈現」
 * （要看得到現值）。`disabled` 在部分瀏覽器會掉對比度、長值讀不清，且不可選取
 * 複製；`readOnly` 仍可聚焦、選取、複製，只是改不動——正好是「唯讀」這件事。
 *
 * `aria-readonly` 是給 AT 的；`data-ccp-readonly` 是給 §6 巡檢腳本數的
 * （判準欄要能機器讀出「這一格已處置」，不能靠人盯畫面）。
 *
 * 不適用 `<Button>`——那是 `ccpDisabledProps()` 的事。react-select 這類沒有
 * `readOnly` 概念的元件用它自己的 `isDisabled`。
 */
export function ccpReadOnlyFieldProps(
    readOnly: boolean,
    options?: { describedBy?: string },
): CcpReadOnlyFieldProps {
    if (!readOnly) return {};
    return {
        readOnly: true,
        'aria-readonly': true,
        'aria-describedby': options?.describedBy ?? CCP_ACCESS_NOTICE_ID,
        'data-ccp-readonly': 'true',
    };
}
