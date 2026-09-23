/**
 * customer-center-platform fork：前端持有的**平台政策副本**（母 repo W2d task 4.1c）。
 *
 * 這個檔**不是**上游 dograh 的一部分（fork-only，W2d task 2.0c）。
 *
 * ## 為什麼要有一份副本（U-22，2026-08-24 拍板）
 *
 * 正本是 `deploy/feature-scope.json`，**在容器外、瀏覽器拿不到**——override 的
 * `ui:` 區段只有 `ports` 與 `environment`，沒有掛載它，也沒有端點把它投影出來。
 *
 * 取「② 硬編 ＋ preflight 比對」而不是「① 閘門新增唯讀端點投影」：
 * 後者會讓客戶瀏覽器多一條可打的路由，需同批進 `route-classification.yml`
 * 與 UI 平面的相容判定，且會使 proposal「唯一觸及母 repo 閘門的改動是
 * `_deny()` 帶 `kind`」變成假。而 `feature-scope.json` 的既有讀者本來就是
 * preflight §7 與 `dograh-bootstrap.py`——② 把比對放在**已經讀它的那一層**，
 * 失效時在**部署期大聲失敗**而非執行期靜默漂移。
 *
 * ## 失效機制（沒有這一條，本檔就只是一份會過期的註解）
 *
 * `deploy/preflight.sh` 有一條比對：本檔的三組常數 vs `deploy/feature-scope.json`，
 * **不一致即 fail**。它讀的是**版控原始檔**，不是執行中的 bundle——
 * 沒有 build 時序依賴，失敗改原始碼即可復原，故放 preflight 正確
 * （與 2.0d「載體 MUST NOT 是 `preflight.sh`」不衝突，準則見 task 4.1c）。
 *
 * **這條比對的是 submodule 工作副本**，未 bump pointer 時仍會綠（gate L-G）；
 * 那一軸由 `2.0b`／`AC8b`／`7.4` 的從零 `git clone --recursive` 承擔。
 *
 * ## 前端要持有的政策知識
 *
 * ① 啟用的工具類型集合 —— `CCP_ALLOWED_TOOL_TYPES`／`CCP_BLOCKED_TOOL_TYPES`
 * ② `transfer_call` 的**欄位層**額外規則 —— `CCP_TOOL_TYPE_REQUIRED_KEYS`。
 *    它**不在** `blocked_tool_types` 裡：`transfer_call` 是允許的類型，
 *    deny 來自 `required_keys` ＋ 遮罩哨兵。**只讀①的話會漏掉它**，
 *    而「UI 說可以選、正本已封鎖」正是 AC5 要消滅的「選了才失敗」。
 * ③ coverage-map 的 deny 清單 —— 不在本檔，落在各頁的 `ccpDisabledProps()`
 *    與 `useCcpPageNotice()`（那是逐頁處置，不是一份可比對的清單）。
 * ④ **W3b（母 repo tasks 3.4）**：transfer 表單的欄位層政策 ——
 *    `CCP_TRANSFER_DEPLOYMENT_KEYS`（六欄，不畫、不送）、
 *    `CCP_TRANSFER_AFTER_HOURS_ACTIONS`／`CCP_TRANSFER_MESSAGE_TYPES`（下拉選項）、
 *    `CCP_TRANSFER_ANNOUNCE_LIMIT_RANGE`（整數範圍）、`CCP_TZ_NAMES`（週表 tz 下拉）、
 *    `CCP_SINGLETON_TOOL_TYPES`（建立對話框維持不可選的獨立宣告）。
 *    **每一項前端驗證在閘門都有對應規則**（`constrained_values` 四種 kind ＋
 *    `forbidden_keys`），本檔的副本只決定畫面。
 *    **全部是扁平字面量陣列**：比對器 `ui-feature-scope-diff.py` 是文字解析，
 *    `_OBJECT_RE` 對巢狀物件會截斷（母 repo review L-1／F-17）——寫成物件會當場失敗。
 *
 * ## 這份副本**不是**執行點
 *
 * 授權的唯一執行點是閘門（`Allowlist.decide()` ＋ admission）。本檔只決定
 * **畫面上長什麼樣**。呈現面失效 MUST NOT 導致授權失效——反過來也一樣：
 * 本檔漏了一項，後果是使用者選得到、送出去被擋，不是擋不住。
 */

import type { ToolCategory } from '@/app/tools/config';

/** `deploy/feature-scope.json` 的 `allowed_tool_types`。 */
export const CCP_ALLOWED_TOOL_TYPES: readonly ToolCategory[] = [
    'transfer_call',
    'end_call',
];

/** `deploy/feature-scope.json` 的 `blocked_tool_types`。 */
export const CCP_BLOCKED_TOOL_TYPES: readonly ToolCategory[] = [
    'calculator',
    'http_api',
    'mcp',
    'native',
    'integration',
];

/**
 * `field_rules.required_keys` ＋ `field_rules.required_keys_when_tool_type`。
 *
 * 形狀刻意做成 map 而不是兩個平行常數：正本是「這些鍵在**這個**工具類型上必填」，
 * 拆成兩個常數的話，日後正本多一個類型時這裡會靜默只覆蓋第一個。
 *
 * ## W3a：改錨，**不是**清空
 *
 * 設定分層把 `queueHealthUrl`／`queueHealthToken` 移出工具設定（改由部署層 env
 * 供給），正本的必要鍵因此**改錨**在留下來的兩條失敗路徑話術上。
 *
 * 天真的「同批清空」在這裡有一個具體的壞結局，而它正是本檔 `:84-88` 逐字警告的
 * 那一格：清空 → `ccpToolTypeAdmission` 回 `selectable: true` → `transfer_call`
 * 在建立對話框裡變成可選 → 預設 definition 帶 `destination` → 命中正本新增的
 * `forbidden_keys` → **送出必然 403**。使用者看到的是「UI 說可以建，建了就失敗」。
 *
 * 改錨之後 `selectable: false` 與 `CCP_DEFAULT_TOOL_CATEGORY` 都**自動維持不變**
 * ——兩者都是集合運算而非硬編值（見下方兩處），所以這一格不需要額外的補償邏輯。
 */
export const CCP_TOOL_TYPE_REQUIRED_KEYS: Readonly<Partial<Record<ToolCategory, readonly string[]>>> = {
    transfer_call: ['transferFailedMessage', 'transferUnavailableMessage'],
};


// ── W3b：transfer 表單的欄位層政策副本（母 repo tasks 3.4）──────────────────
//
// 正本：`deploy/feature-scope.json`。每一個常數都由 `ui-feature-scope-diff.py`
// 與正本比對；**改這裡而不改正本（或反之）會在 preflight 當場失敗**。

/**
 * `transfer_call` 的部署層六欄（正本 `forbidden_keys` 的子集；母 repo
 * `feature_scope_check.DEPLOYMENT_ENV_FIELDS` 的鍵順序）。
 * 表單**不畫輸入框、不顯示值、送出 body 永不含這些鍵**（含 `null` 形式——
 * `forbidden_keys` 是存在性判定）。
 */
export const CCP_TRANSFER_DEPLOYMENT_KEYS = [
    'destination',
    'alternateDestination',
    'queueHealthUrl',
    'queueHealthToken',
    'queueHealthTimeoutSeconds',
    'queueHealthCacheTtlSeconds',
] as const;

/** 正本 `constrained_values.afterHoursAction.enum_values`（執行層 `_SUPPORTED_AFTER_HOURS` 的複本）。 */
export const CCP_TRANSFER_AFTER_HOURS_ACTIONS = [
    'back_to_ai',
    'announce_and_hangup',
    'alternate_queue',
] as const;

/** 正本 `constrained_values.messageType.enum_values`（上游 `Literal` 的複本）。 */
export const CCP_TRANSFER_MESSAGE_TYPES = ['none', 'custom', 'audio'] as const;

/** 正本 `constrained_values.unavailableAnnounceLimit` 的 `[min, max]`。 */
export const CCP_TRANSFER_ANNOUNCE_LIMIT_RANGE = [1, 10] as const;

/**
 * 建立對話框維持不可選的型別（正本 `field_rules.singleton_tool_types`）。
 * 這是「呈現面比授權面更嚴」的**刻意例外**：閘門對實施方 `POST /tools/` 放行，
 * 擋在對話框只是體驗面。理由 R-AE（一套部署一組部署層值）。
 */
export const CCP_SINGLETON_TOOL_TYPES: readonly ToolCategory[] = ['transfer_call'];

/**
 * 週表 `tz` 下拉的選項：正本 `constrained_values.schedule.tz_names` 的**同一份快照**
 * （IANA 名稱，自執行層映像 ccp-dograh-api 產生、剔除 localtime 與 Factory——review F-1／security M-2）。**不用** `Intl.supportedValuesOf('timeZone')`——
 * 那是瀏覽器的集合，與閘門的判準可能不同，選得到卻存不進去。
 */
export const CCP_TZ_NAMES = [
    'Africa/Abidjan', 'Africa/Accra', 'Africa/Addis_Ababa', 'Africa/Algiers', 'Africa/Asmara',
    'Africa/Bamako', 'Africa/Bangui', 'Africa/Banjul', 'Africa/Bissau', 'Africa/Blantyre',
    'Africa/Brazzaville', 'Africa/Bujumbura', 'Africa/Cairo', 'Africa/Casablanca', 'Africa/Ceuta',
    'Africa/Conakry', 'Africa/Dakar', 'Africa/Dar_es_Salaam', 'Africa/Djibouti', 'Africa/Douala',
    'Africa/El_Aaiun', 'Africa/Freetown', 'Africa/Gaborone', 'Africa/Harare', 'Africa/Johannesburg',
    'Africa/Juba', 'Africa/Kampala', 'Africa/Khartoum', 'Africa/Kigali', 'Africa/Kinshasa',
    'Africa/Lagos', 'Africa/Libreville', 'Africa/Lome', 'Africa/Luanda', 'Africa/Lubumbashi',
    'Africa/Lusaka', 'Africa/Malabo', 'Africa/Maputo', 'Africa/Maseru', 'Africa/Mbabane', 'Africa/Mogadishu',
    'Africa/Monrovia', 'Africa/Nairobi', 'Africa/Ndjamena', 'Africa/Niamey', 'Africa/Nouakchott',
    'Africa/Ouagadougou', 'Africa/Porto-Novo', 'Africa/Sao_Tome', 'Africa/Timbuktu', 'Africa/Tripoli',
    'Africa/Tunis', 'Africa/Windhoek', 'America/Adak', 'America/Anchorage', 'America/Anguilla',
    'America/Antigua', 'America/Araguaina', 'America/Argentina/Buenos_Aires', 'America/Argentina/Catamarca',
    'America/Argentina/Cordoba', 'America/Argentina/Jujuy', 'America/Argentina/La_Rioja', 'America/Argentina/Mendoza',
    'America/Argentina/Rio_Gallegos', 'America/Argentina/Salta', 'America/Argentina/San_Juan',
    'America/Argentina/San_Luis', 'America/Argentina/Tucuman', 'America/Argentina/Ushuaia',
    'America/Aruba', 'America/Asuncion', 'America/Atikokan', 'America/Atka', 'America/Bahia',
    'America/Bahia_Banderas', 'America/Barbados', 'America/Belem', 'America/Belize', 'America/Blanc-Sablon',
    'America/Boa_Vista', 'America/Bogota', 'America/Boise', 'America/Cambridge_Bay', 'America/Campo_Grande',
    'America/Cancun', 'America/Caracas', 'America/Cayenne', 'America/Cayman', 'America/Chicago',
    'America/Chihuahua', 'America/Ciudad_Juarez', 'America/Coral_Harbour', 'America/Costa_Rica',
    'America/Coyhaique', 'America/Creston', 'America/Cuiaba', 'America/Curacao', 'America/Danmarkshavn',
    'America/Dawson', 'America/Dawson_Creek', 'America/Denver', 'America/Detroit', 'America/Dominica',
    'America/Edmonton', 'America/Eirunepe', 'America/El_Salvador', 'America/Ensenada', 'America/Fort_Nelson',
    'America/Fortaleza', 'America/Glace_Bay', 'America/Goose_Bay', 'America/Grand_Turk', 'America/Grenada',
    'America/Guadeloupe', 'America/Guatemala', 'America/Guayaquil', 'America/Guyana', 'America/Halifax',
    'America/Havana', 'America/Hermosillo', 'America/Indiana/Indianapolis', 'America/Indiana/Knox',
    'America/Indiana/Marengo', 'America/Indiana/Petersburg', 'America/Indiana/Tell_City', 'America/Indiana/Vevay',
    'America/Indiana/Vincennes', 'America/Indiana/Winamac', 'America/Inuvik', 'America/Iqaluit',
    'America/Jamaica', 'America/Juneau', 'America/Kentucky/Louisville', 'America/Kentucky/Monticello',
    'America/Kralendijk', 'America/La_Paz', 'America/Lima', 'America/Los_Angeles', 'America/Lower_Princes',
    'America/Maceio', 'America/Managua', 'America/Manaus', 'America/Marigot', 'America/Martinique',
    'America/Matamoros', 'America/Mazatlan', 'America/Menominee', 'America/Merida', 'America/Metlakatla',
    'America/Mexico_City', 'America/Miquelon', 'America/Moncton', 'America/Monterrey', 'America/Montevideo',
    'America/Montreal', 'America/Montserrat', 'America/Nassau', 'America/New_York', 'America/Nipigon',
    'America/Nome', 'America/Noronha', 'America/North_Dakota/Beulah', 'America/North_Dakota/Center',
    'America/North_Dakota/New_Salem', 'America/Nuuk', 'America/Ojinaga', 'America/Panama',
    'America/Pangnirtung', 'America/Paramaribo', 'America/Phoenix', 'America/Port-au-Prince',
    'America/Port_of_Spain', 'America/Porto_Acre', 'America/Porto_Velho', 'America/Puerto_Rico',
    'America/Punta_Arenas', 'America/Rainy_River', 'America/Rankin_Inlet', 'America/Recife',
    'America/Regina', 'America/Resolute', 'America/Rio_Branco', 'America/Santa_Isabel', 'America/Santarem',
    'America/Santiago', 'America/Santo_Domingo', 'America/Sao_Paulo', 'America/Scoresbysund',
    'America/Shiprock', 'America/Sitka', 'America/St_Barthelemy', 'America/St_Johns', 'America/St_Kitts',
    'America/St_Lucia', 'America/St_Thomas', 'America/St_Vincent', 'America/Swift_Current',
    'America/Tegucigalpa', 'America/Thule', 'America/Thunder_Bay', 'America/Tijuana', 'America/Toronto',
    'America/Tortola', 'America/Vancouver', 'America/Virgin', 'America/Whitehorse', 'America/Winnipeg',
    'America/Yakutat', 'America/Yellowknife', 'Antarctica/Casey', 'Antarctica/Davis', 'Antarctica/DumontDUrville',
    'Antarctica/Macquarie', 'Antarctica/Mawson', 'Antarctica/McMurdo', 'Antarctica/Palmer',
    'Antarctica/Rothera', 'Antarctica/Syowa', 'Antarctica/Troll', 'Antarctica/Vostok', 'Arctic/Longyearbyen',
    'Asia/Aden', 'Asia/Almaty', 'Asia/Amman', 'Asia/Anadyr', 'Asia/Aqtau', 'Asia/Aqtobe', 'Asia/Ashgabat',
    'Asia/Atyrau', 'Asia/Baghdad', 'Asia/Bahrain', 'Asia/Baku', 'Asia/Bangkok', 'Asia/Barnaul',
    'Asia/Beirut', 'Asia/Bishkek', 'Asia/Brunei', 'Asia/Chita', 'Asia/Chongqing', 'Asia/Colombo',
    'Asia/Damascus', 'Asia/Dhaka', 'Asia/Dili', 'Asia/Dubai', 'Asia/Dushanbe', 'Asia/Famagusta',
    'Asia/Gaza', 'Asia/Harbin', 'Asia/Hebron', 'Asia/Ho_Chi_Minh', 'Asia/Hong_Kong', 'Asia/Hovd',
    'Asia/Irkutsk', 'Asia/Istanbul', 'Asia/Jakarta', 'Asia/Jayapura', 'Asia/Jerusalem', 'Asia/Kabul',
    'Asia/Kamchatka', 'Asia/Karachi', 'Asia/Kashgar', 'Asia/Kathmandu', 'Asia/Khandyga', 'Asia/Kolkata',
    'Asia/Krasnoyarsk', 'Asia/Kuala_Lumpur', 'Asia/Kuching', 'Asia/Kuwait', 'Asia/Macau', 'Asia/Magadan',
    'Asia/Makassar', 'Asia/Manila', 'Asia/Muscat', 'Asia/Nicosia', 'Asia/Novokuznetsk', 'Asia/Novosibirsk',
    'Asia/Omsk', 'Asia/Oral', 'Asia/Phnom_Penh', 'Asia/Pontianak', 'Asia/Pyongyang', 'Asia/Qatar',
    'Asia/Qostanay', 'Asia/Qyzylorda', 'Asia/Riyadh', 'Asia/Sakhalin', 'Asia/Samarkand', 'Asia/Seoul',
    'Asia/Shanghai', 'Asia/Singapore', 'Asia/Srednekolymsk', 'Asia/Taipei', 'Asia/Tashkent',
    'Asia/Tbilisi', 'Asia/Tehran', 'Asia/Tel_Aviv', 'Asia/Thimphu', 'Asia/Tokyo', 'Asia/Tomsk',
    'Asia/Ulaanbaatar', 'Asia/Urumqi', 'Asia/Ust-Nera', 'Asia/Vientiane', 'Asia/Vladivostok',
    'Asia/Yakutsk', 'Asia/Yangon', 'Asia/Yekaterinburg', 'Asia/Yerevan', 'Atlantic/Azores',
    'Atlantic/Bermuda', 'Atlantic/Canary', 'Atlantic/Cape_Verde', 'Atlantic/Faroe', 'Atlantic/Jan_Mayen',
    'Atlantic/Madeira', 'Atlantic/Reykjavik', 'Atlantic/South_Georgia', 'Atlantic/St_Helena',
    'Atlantic/Stanley', 'Australia/Adelaide', 'Australia/Brisbane', 'Australia/Broken_Hill',
    'Australia/Canberra', 'Australia/Currie', 'Australia/Darwin', 'Australia/Eucla', 'Australia/Hobart',
    'Australia/Lindeman', 'Australia/Lord_Howe', 'Australia/Melbourne', 'Australia/Perth',
    'Australia/Sydney', 'Australia/Yancowinna', 'Etc/GMT', 'Etc/GMT+0', 'Etc/GMT+1', 'Etc/GMT+10',
    'Etc/GMT+11', 'Etc/GMT+12', 'Etc/GMT+2', 'Etc/GMT+3', 'Etc/GMT+4', 'Etc/GMT+5', 'Etc/GMT+6',
    'Etc/GMT+7', 'Etc/GMT+8', 'Etc/GMT+9', 'Etc/GMT-0', 'Etc/GMT-1', 'Etc/GMT-10', 'Etc/GMT-11',
    'Etc/GMT-12', 'Etc/GMT-13', 'Etc/GMT-14', 'Etc/GMT-2', 'Etc/GMT-3', 'Etc/GMT-4', 'Etc/GMT-5',
    'Etc/GMT-6', 'Etc/GMT-7', 'Etc/GMT-8', 'Etc/GMT-9', 'Etc/GMT0', 'Etc/Greenwich', 'Etc/UCT',
    'Etc/UTC', 'Etc/Universal', 'Etc/Zulu', 'Europe/Amsterdam', 'Europe/Andorra', 'Europe/Astrakhan',
    'Europe/Athens', 'Europe/Belfast', 'Europe/Belgrade', 'Europe/Berlin', 'Europe/Bratislava',
    'Europe/Brussels', 'Europe/Bucharest', 'Europe/Budapest', 'Europe/Busingen', 'Europe/Chisinau',
    'Europe/Copenhagen', 'Europe/Dublin', 'Europe/Gibraltar', 'Europe/Guernsey', 'Europe/Helsinki',
    'Europe/Isle_of_Man', 'Europe/Istanbul', 'Europe/Jersey', 'Europe/Kaliningrad', 'Europe/Kirov',
    'Europe/Kyiv', 'Europe/Lisbon', 'Europe/Ljubljana', 'Europe/London', 'Europe/Luxembourg',
    'Europe/Madrid', 'Europe/Malta', 'Europe/Mariehamn', 'Europe/Minsk', 'Europe/Monaco', 'Europe/Moscow',
    'Europe/Nicosia', 'Europe/Oslo', 'Europe/Paris', 'Europe/Podgorica', 'Europe/Prague', 'Europe/Riga',
    'Europe/Rome', 'Europe/Samara', 'Europe/San_Marino', 'Europe/Sarajevo', 'Europe/Saratov',
    'Europe/Simferopol', 'Europe/Skopje', 'Europe/Sofia', 'Europe/Stockholm', 'Europe/Tallinn',
    'Europe/Tirane', 'Europe/Tiraspol', 'Europe/Ulyanovsk', 'Europe/Vaduz', 'Europe/Vatican',
    'Europe/Vienna', 'Europe/Vilnius', 'Europe/Volgograd', 'Europe/Warsaw', 'Europe/Zagreb',
    'Europe/Zurich', 'GMT', 'Indian/Antananarivo', 'Indian/Chagos', 'Indian/Christmas', 'Indian/Cocos',
    'Indian/Comoro', 'Indian/Kerguelen', 'Indian/Mahe', 'Indian/Maldives', 'Indian/Mauritius',
    'Indian/Mayotte', 'Indian/Reunion', 'Pacific/Apia', 'Pacific/Auckland', 'Pacific/Bougainville',
    'Pacific/Chatham', 'Pacific/Chuuk', 'Pacific/Easter', 'Pacific/Efate', 'Pacific/Fakaofo',
    'Pacific/Fiji', 'Pacific/Funafuti', 'Pacific/Galapagos', 'Pacific/Gambier', 'Pacific/Guadalcanal',
    'Pacific/Guam', 'Pacific/Honolulu', 'Pacific/Johnston', 'Pacific/Kanton', 'Pacific/Kiritimati',
    'Pacific/Kosrae', 'Pacific/Kwajalein', 'Pacific/Majuro', 'Pacific/Marquesas', 'Pacific/Midway',
    'Pacific/Nauru', 'Pacific/Niue', 'Pacific/Norfolk', 'Pacific/Noumea', 'Pacific/Pago_Pago',
    'Pacific/Palau', 'Pacific/Pitcairn', 'Pacific/Pohnpei', 'Pacific/Port_Moresby', 'Pacific/Rarotonga',
    'Pacific/Saipan', 'Pacific/Samoa', 'Pacific/Tahiti', 'Pacific/Tarawa', 'Pacific/Tongatapu',
    'Pacific/Wake', 'Pacific/Wallis', 'Pacific/Yap', 'UTC',
] as const;

export interface CcpToolTypeAdmission {
    /** 這個類型能不能在「建立工具」對話框裡選。 */
    selectable: boolean;
    /** 不能選（或選了也會失敗）的原因，繁中，直接上畫面。空字串＝沒有話要說。 */
    reason: string;
}

/**
 * 一個工具類型在本部署的建立面待遇。
 *
 * **三種結果，不是兩種**：
 *   - 封鎖類型 → 不可選，原因是「本部署未開放這個類型」。
 *   - 允許但**預設 definition 湊不齊必要鍵**（今天只有 `transfer_call`）
 *     → 也不可選，但原因完全不同：能力有、只是不從這裡建。
 *     N3 要求這一格要**標明**，因為它是最容易被讀成 bug 的一格
 *     （UI 讓你選、送出去卻必然 403）。
 *   - 其餘允許類型 → 可選。
 *
 * **不寫死數字**（gate L-2／T16）：`TOOL_CATEGORIES` 現為 7 筆、其中 `native`
 * 與 `integration` 上游已帶 `disabled`，任何「五種／三種」式的敘述都對不上，
 * 且上游一改就錯。判準是集合運算，不是計數。
 */
export function ccpToolTypeAdmission(category: ToolCategory): CcpToolTypeAdmission {
    if (CCP_BLOCKED_TOOL_TYPES.includes(category)) {
        return {
            selectable: false,
            reason: '本部署未開放這個工具類型：交付範圍是話術與轉接設定的維護。',
        };
    }
    // W3b（母 repo review F-10）：單例型別**先於**必要鍵推導。表單補齊後
    // 「必要欄位新建時湊不齊」對 transfer_call 已不成立，真理由是 R-AE。
    if (CCP_SINGLETON_TOOL_TYPES.includes(category)) {
        return {
            selectable: false,
            reason:
                '本部署只能有一個轉接工具，且由建置程序建立；'
                + '話術與營業時間請到既有的轉接工具頁面調整。',
        };
    }
    const requiredKeys = CCP_TOOL_TYPE_REQUIRED_KEYS[category];
    if (requiredKeys && requiredKeys.length > 0) {
        return {
            selectable: false,
            reason:
                `${category} 的必要欄位（${requiredKeys.join('、')}）新建時湊不齊，`
                + '送出會被內容檢查擋下。'
                + '轉接工具已由建置單位配置好，需要調整請與您的專案窗口提出。',
        };
    }
    return { selectable: true, reason: '' };
}

/**
 * 建立對話框的預設類型（task 4.1b）。
 *
 * 上游預設是 `http_api`，而它在 `blocked_tool_types` 內——只做「不可選」會留下
 * 一個**當前值即為 disabled 項**的 Select（選單打開全灰、關起來卻顯示一個
 * 選不回去的值）。取第一個 `selectable` 的類型，**不硬編**：正本改了就跟著改，
 * 而 preflight 的比對保證正本與本檔同步。
 */
export const CCP_DEFAULT_TOOL_CATEGORY: ToolCategory =
    CCP_ALLOWED_TOOL_TYPES.find((c) => ccpToolTypeAdmission(c).selectable) ?? 'end_call';
