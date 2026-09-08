"""Workflow-level ``transfer_call`` tool config lookup (shared).

The transfer gate's inputs — business-hours schedule and queue-health keys —
live in the workflow's ``transfer_call`` tool config. Two consumers need it:
the in-call engine (voice tool / press-0, via
``PipecatEngine.resolve_transfer_call_config``) and the engine-less capacity
overflow chain (S-L9-SCALE), which has a resolved workflow but no run. One
lookup so the two can never disagree on where the gate config comes from.

**W3a — the config now has two layers.** Six keys (``destination``,
``alternateDestination``, ``queueHealthUrl``, ``queueHealthToken``,
``queueHealthTimeoutSeconds``, ``queueHealthCacheTtlSeconds``) are *deployment
layer*: their value is decided by the deployment site, not by whoever writes
the script, so they are supplied by environment and overwrite whatever the tool
definition carries. The other ten are *speech layer* and stay in the definition.
:func:`deployment_transfer_config` reads the former; :func:`revalidate_transfer_config`
merges them in **before** it validates, and :func:`validate_transfer_config`
checks them once at boot. Why here and not in
:func:`find_transfer_call_config`: there are **three** readers of this config,
and only two of them go through that lookup — see the note on
:func:`revalidate_transfer_config`.
"""

import math
import os
from urllib.parse import urlsplit

from loguru import logger

from api.db import db_client
from api.enums import ToolCategory

# Health-probe URLs the gate is allowed to call. The canonical, richer rule
# (allowlisted hosts, no userinfo, no IDN, explicit ports) is
# ``feature_scope_check._check_url`` in the platform repo — it runs at
# deployment time and is **not** mounted into this container. What is checked
# here is the subset that matters at call time: is this a thing we are willing
# to make an outbound request to at all.
_HEALTH_URL_SCHEMES = ("http", "https")


def _health_url_problem(value: str) -> str | None:
    """Minimal call-time shape check for ``queueHealthUrl``. None == usable.

    **Never raises.** ``urlsplit`` itself throws ``ValueError`` on some inputs
    (``http://[bad]/health`` — "does not appear to be an IPv4 or IPv6 address"),
    and a raise here does not degrade the way the caller's per-field design
    intends: it escapes ``revalidate_transfer_config`` entirely, so instead of
    "drop the two health keys" the voice handler reports a generic
    ``execution_error`` and the capacity gate's ``except`` around the lookup
    degrades to an **empty config** — schedule gate and queue-health gate both
    silently off. That is the same failure shape review B-1/M-5 closed, arriving
    through a different door (Codex review, 2026-08-20).
    """
    if any(ch.isspace() for ch in value):
        return "contains whitespace"
    try:
        parts = urlsplit(value)
        # ``.port`` is read here on purpose: ``urlsplit`` itself does not raise
        # on ``http://queue:99999/health`` or ``:abc`` — the *property* does,
        # lazily. Leaving it out split one family of config typo into two
        # opposite behaviours (review M-1): ``[bad]`` dropped the two probe
        # keys and let the transfer proceed, while a bad port was declared
        # usable and then failed inside ``queue_is_healthy``, which swallows it
        # and caches ``healthy=False`` → **every in-hours transfer refused**
        # for the TTL. That is the louder failure, and it was the unhandled one.
        scheme, netloc, hostname = parts.scheme, parts.netloc, parts.hostname
        parts.port
    except ValueError:
        # Deliberately no detail and no value: this string reaches a call log,
        # and the parser's own message quotes the input.
        return "is not parseable as a URL"
    if scheme not in _HEALTH_URL_SCHEMES:
        return "scheme is not http/https"
    if "@" in netloc:
        return "carries userinfo"
    if not hostname:
        return "has no host"
    return None


# ── 部署層（W3a D1／D2）──────────────────────────────────────────────────
# 鍵 → env 變數名，依 D1 的六欄順序。``DOGRAH_TRANSFER_DESTINATION`` 與
# ``QUEUE_HEALTH_TOKEN`` 沿用今日 ``reception.json`` 的 ``${ENV:...}`` 佔位符所
# 引用的名字（該範本於 W3a §2.1 移除這些鍵，變數名不變），其餘四個為新增。
_DEPLOYMENT_ENV_KEYS: tuple[tuple[str, str], ...] = (
    ("destination", "DOGRAH_TRANSFER_DESTINATION"),
    ("alternateDestination", "DOGRAH_TRANSFER_ALTERNATE_DESTINATION"),
    ("queueHealthUrl", "QUEUE_HEALTH_URL"),
    ("queueHealthToken", "QUEUE_HEALTH_TOKEN"),
    ("queueHealthTimeoutSeconds", "QUEUE_HEALTH_TIMEOUT_SECONDS"),
    ("queueHealthCacheTtlSeconds", "QUEUE_HEALTH_CACHE_TTL_SECONDS"),
)

_NUMERIC_DEPLOYMENT_KEYS = frozenset(
    {"queueHealthTimeoutSeconds", "queueHealthCacheTtlSeconds"}
)

#: The floor applies to the **probe budget only** (platform review gate M2).
#: ``queueHealthCacheTtlSeconds`` is a cache lifetime, not a probe budget:
#: setting it to 0 disables the cache, it cannot cause a timeout, and upstream
#: ``queue_health._bounded_seconds`` documents that explicitly -- "An explicit 0
#: is honored (TTL 0 disables the cache)". Applying the 0.2s floor to it made a
#: supported input block boot, with a message about probe budgets that does not
#: describe what the field does. The platform side mirrors this split in
#: ``feature_scope_check._FLOORED_SECONDS_FIELDS`` -- CHANGE BOTH TOGETHER.
_FLOORED_SECONDS_KEYS = frozenset({"queueHealthTimeoutSeconds"})


def deployment_transfer_config() -> dict:
    """部署層供給的轉接設定，只含**實際供給**的鍵。缺值不入結果、不拋例外。

    **每次呼叫都讀 ``os.environ``**（D2）。MUST NOT 在 import 期讀進模組常數：
    D5 宣稱「憑證輪替只要改 env 就生效、**不需要 re-apply 話術**」，而 import 期讀
    會讓一個已經起來的行程再也看不到新值。

    **這買到的不是「不必重建容器」**（platform review gate F-8 更正）：行程的
    environ 在容器建立時就固定，``docker restart`` 沿用同一個容器帶著舊值起來，
    所以套用新的 ``.env`` 一定要 ``up -d --force-recreate``。per-call 讀 environ
    的收益是**工作流定義完全不必碰**，以及同一個行程內先後兩次解析看得到新值。

    空字串與純空白視同未供給，與 ``fallback_queue()``／``overflow_transfer_to()``
    的既有慣例一致（``.env`` 裡一個沒填值的鍵是「沒設定」，不是「設定成空字串」）。

    兩個秒數欄位能轉 float 就轉，轉不動就**原樣保留字串**：下游
    ``queue_health._bounded_seconds`` 對兩者都寬容（junk → 用預設值，永不拋），
    而在這裡拋例外會讓一個打錯的 env 值變成每通電話的例外。形狀不合的回報由
    :func:`validate_transfer_config` 在開機期負責，那才是它該大聲的地方。
    """
    supplied: dict = {}
    for key, env_name in _DEPLOYMENT_ENV_KEYS:
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        value = raw.strip()
        if not value:
            continue
        if key in _NUMERIC_DEPLOYMENT_KEYS:
            try:
                supplied[key] = float(value)
                continue
            except ValueError:
                pass
        supplied[key] = value
    return supplied


def _merge_deployment_layer(config: dict) -> dict:
    """部署層**無條件**勝出：供給即覆蓋，未供給即讓該鍵不存在。

    **覆蓋方向不可反轉**：``fallback``（資料庫有值就用資料庫）會讓一次經編輯器的
    寫入永久壓過部署層，憑證輪替再度失效——那正是分層要消除的失效。

    **遷移期的 DB fallback 已於 W3a §5.1 移除**（D13 的限期例外到期）。移除之後
    未供給的鍵**不會**留下資料庫內的殘值：那個殘值可能是分層之前的舊憑證、也可能
    是經某條寫入路徑塞進來的目的地，而「部署層覆蓋一切」正是分層的整個防護論述。
    缺值的可見性由 :func:`validate_transfer_config` 在開機期擋下——**兩者同批**，
    只做其中一件都會留下缺口：只移 fallback 而不擋開機，缺值時靜默無值
    （``queue_is_healthy`` 的 URL 缺席是 fail-open，健康閘整個消失）。
    """
    supplied = deployment_transfer_config()
    merged = dict(config)
    for key, _env_name in _DEPLOYMENT_ENV_KEYS:
        if key in supplied:
            merged[key] = supplied[key]
        else:
            merged.pop(key, None)
    return merged


# ── 開機期驗證（W3a D10）────────────────────────────────────────────────
# 健康探測秒數的下界。上游 ``queue_health._bounded_seconds`` 只 clamp **上界**
# （2.0／60.0）並拒負值，於是顯式的 ``0.001`` 會被誠實採用 → 探測必逾時 →
# 恆判不健康 → **營運時間內的真人轉接全滅**（W2c review M-6）。
#
# **這個數字在 ``deploy/bin/feature_scope_check.py`` 有一份刻意的複本**
# （常數名 ``MIN_PROBE_SECONDS``；W3a §2.7）：那一份是部署期的擋門，這一份是
# 開機期的。兩處都要，理由與 D10 相同——preflight 有已知繞道且是一次性，
# 而本檔看得到行程實際讀到的值。改一處 SHALL 同批改另一處。
#
# **交叉引用原本指錯檔**（W3a §9.3 reviewer L-19）：寫的是 ``deploy/preflight.sh``
# ——那支只是**呼叫**驗證器，常數不在它裡面。照著指標去找的人找不到東西，
# 於是「同批改」這個承諾在第一步就斷了。兩份現由平台側的
# ``test_both_copies_of_the_probe_floor_agree`` 斷言一致（願望改成守衛）。
_MIN_PROBE_SECONDS = 0.2


def validate_transfer_config() -> None:
    """開機期檢查部署層供給的 6 個值（W3a D10）。

    鏡像 ``validate_safetynet_config``（``livekit_safetynet.py``）與
    ``validate_capacity_config``（``capacity_gate.py``）：同一類「由 env 承載的
    轉接目的地」，本 repo 已有成熟先例，兩者都是不合格直接 ``RuntimeError``。

    **為什麼非有不可。** 六欄移出 ``definition.config`` 之後，「這些值有沒有被
    供給」的執行點自三個（preflight／bootstrap prescan／gateway admission）降為
    **preflight 一個**，而它是部署期一次性、且有已知繞道（RUNBOOK 記載的手動
    一次性容器、指向他處的 ``DOGRAH_WORKFLOW_DIR``），繞過即零執行點。而未供給的
    後果不是「功能沒開」而是**控制靜默消失**：``queue_is_healthy`` 在 URL 未設定時
    ``return True``（fail-open），隊列健康閘整個不見，每位要求真人的來電者被 REFER
    進一個可能已死的隊列。

    **通話期的 ``revalidate`` 不算等效執行點**：它對不合格值的處置是把
    ``destination`` 抹白、逐通降級，不是大聲失敗，而且它每通重複一次無人看見的降級。

    檢查四類：存在性、形狀（含高費率號段）、``queueHealthUrl`` 的 scheme／host
    白名單、兩個秒數的數值合法性與下界。

    **白名單的差額是宣告過的，不是遺漏**：正本的 ``_check_url``（userinfo、IDN、
    尾隨點、顯式埠）住在 ``deploy/bin/feature_scope_check.py``，**沒有** bind-mount
    進本容器——掛進來的只有 JSON。故這裡讀同一份 JSON 的規則、套用做得到的子集
    （scheme ＋ ``host:port``），完整那一份由 ``preflight.sh`` 對同一組 env 值執行
    （W3a §2.6）。

    **不合格即 ``RuntimeError``**（W3a §5.2；遷移期的警告模式已到期）。

    **但「檢查跑不成」與「值不合格」分開處置**（§5.2 落地時發現的缺口）。本函式有
    兩條「跑不成」的路徑，兩者都是少掉一個 ``-v``：共用的 REFER URI 解析器沒掛進來、
    啟用集合正本沒掛進來。收緊之後若把它們一併算成不合格，一個漏掛的掛載就會讓
    ``dograh-api`` 起不來——**平台整個停止接聽電話**，而 D-A5 對這一取捨已有明確
    結論：一個少掉的 ``-v`` MUST NOT 變成「不啟動」。故：

    - **值不合格 → 擋開機**（缺值、形狀不合、命中高費率、不在白名單、秒數越界）。
    - **檢查不可用 → 大聲 log、不擋開機**，並在訊息裡說明它**沒有**被驗過。
      這不是「缺檔當成沒東西要檢查」的翻版：掛載本身在部署期有執行點
      （``preflight.sh`` 讀 compose 渲染結果驗掛載），而值本身也已被 preflight
      對同一組 env 驗過一次。這裡失去的是開機期的第二道，不是唯一一道。
    """
    problems: list[str] = []
    unverifiable: list[str] = []
    supplied = deployment_transfer_config()

    # ① 存在性。三個鍵的缺席各自關掉一個控制，故逐鍵指名而不是「有幾個沒設」。
    for key, env_name in _DEPLOYMENT_ENV_KEYS:
        if key in ("alternateDestination",):
            # 非營運替代目的地是選填：未設定＝不走 alternate_queue 分支，
            # 那是一個有效的部署形態，不是缺陷。
            continue
        if key in supplied:
            continue
        if key in _NUMERIC_DEPLOYMENT_KEYS:
            # 未設定＝採 queue_health 的預設值（0.5／5.0），是合法形態。
            continue
        problems.append(f"{env_name} is not set")

    # ② 目的地形狀與高費率號段。兩個欄位同型、同一份解析器。
    destinations = [
        (key, env_name, supplied[key])
        for key, env_name in _DEPLOYMENT_ENV_KEYS
        if key in ("destination", "alternateDestination") and key in supplied
    ]
    if destinations:
        from api.services.pipecat.capacity_gate import (
            PREMIUM_RATE_PREFIXES,
            _premium_rate,
        )
        from api.services.platform_scope import (
            PlatformArtifactMissing,
            log_artifact_missing,
            parse_refer_uri,
        )

        for key, env_name, value in destinations:
            # **``try`` 只包解析那一句**（platform review gate F-6）。原本它包住
            # 整個迴圈，於是第一個目的地拋 ``PlatformArtifactMissing`` 就讓迴圈
            # 整個中止——連 ``_premium_rate`` 都沒跑，而那個檢查刻意做了
            # ``_legacy_premium_candidates`` 兜底、**不需要解析器也能跑**、且不會拋。
            # 結果是 ``sip_uri.py`` 漏掛時 ``tel:+19005551212`` 開機全綠。通話期
            # 有兜底所以不會真的撥出去，但**運維據以行動的那份開機報告是錯的**，
            # 而它同時代表所有轉接都失效。
            try:
                parsed = parse_refer_uri(value)
            except PlatformArtifactMissing as exc:
                # 缺 mount 不擋開機（D-A5 的既有取捨：一個少掉的 ``-v`` MUST NOT
                # 變成「dograh-api 不啟動」＝平台停止接聽電話）。
                log_artifact_missing("validate_transfer_config", exc)
                unverifiable.append(
                    f"{env_name} could not be shape-checked: the shared REFER URI "
                    "parser is not mounted"
                )
            else:
                if not parsed.ok:
                    # ``parsed.reason`` 依該模組契約不含輸入的任何片段——這個值
                    # 可能是客戶號碼或內部 PBX 主機，而本訊息會進啟動日誌。
                    problems.append(
                        f"{env_name} is not a valid REFER target: {parsed.reason}"
                    )
                    continue

            # 高費率判定**無條件執行**：它不依賴解析器，而它擋的是最貴的失效。
            if _premium_rate(value):
                problems.append(
                    f"{env_name} matches a premium-rate prefix {PREMIUM_RATE_PREFIXES}"
                )

    # ③ queueHealthUrl 的 scheme／host 白名單。
    health_url = supplied.get("queueHealthUrl")
    if health_url:
        problem = _health_url_problem(str(health_url))
        if problem:
            problems.append(f"QUEUE_HEALTH_URL {problem}")
        else:
            verdicts, unchecked = _allowlist_problems(str(health_url))
            problems.extend(verdicts)
            unverifiable.extend(unchecked)

    # ④ 兩個秒數：數值合法性與下界。
    for key, env_name in _DEPLOYMENT_ENV_KEYS:
        if key not in _NUMERIC_DEPLOYMENT_KEYS or key not in supplied:
            continue
        value = supplied[key]
        if not isinstance(value, float):
            # deployment_transfer_config 轉不動就原樣留字串——那就是「不是數字」。
            problems.append(f"{env_name} is not a number: {value!r}")
            continue
        # Non-finite first (platform review gate F-3). ``float("nan")`` parses
        # fine and NaN compares False against *every* operator, so ``nan`` slips
        # past the floor below without a word -- and then
        # ``asyncio.wait_for(timeout=nan)`` raises TimeoutError immediately,
        # which is exactly the failure this floor exists to prevent, in its most
        # complete form. ``inf`` is only clamped upstream, never rejected.
        #
        # The platform side carries the same check in
        # ``feature_scope_check.check_deployment_env``; that copy is deliberate
        # (no shared carrier across the two repos) -- CHANGE BOTH TOGETHER.
        if not math.isfinite(value):
            problems.append(
                f"{env_name} is {value!r}, not a finite number; nan slips past "
                "both the floor and the upstream cap because every comparison "
                "against NaN is False, and a nan timeout fails instantly -- "
                "pinning the health verdict to unhealthy and refusing every "
                "in-hours transfer"
            )
            continue
        if value < 0:
            problems.append(
                f"{env_name} is {value}, negative; upstream _bounded_seconds "
                "silently falls back to its default, so the configured value and "
                "the effective value disagree with nothing saying so"
            )
        elif key in _FLOORED_SECONDS_KEYS and value < _MIN_PROBE_SECONDS:
            problems.append(
                f"{env_name} is {value}, below the {_MIN_PROBE_SECONDS}s floor; "
                "a probe budget this small times out every time, which pins the "
                "health verdict to unhealthy and refuses every in-hours transfer"
            )

    # 「沒驗成」永遠說出來，**且在拋例外之前說**：不合格與沒驗成可能同時發生，
    # 而 RuntimeError 只帶得走前者。先 log 才不會讓後者被前者吃掉。
    for item in unverifiable:
        _config_event(
            "transfer.deploy_config_unverified",
            f"transfer.deploy_config_unverified: {item} "
            "(boot continues by design — a missing bind mount must not take the "
            "platform off the air; this value was NOT checked at boot)",
        )

    if not problems:
        return

    raise RuntimeError(
        "transfer deployment config is not usable: " + "; ".join(problems)
    )


def _config_event(
    event: str, message: str, *, field: str = "", level: str = "error"
) -> None:
    """記結構化事件**並**送進告警通道（platform review gate F-12）。

    ``logger.bind(call_event=...)`` 只給了事件的**形狀**，沒有給它的**路徑**：
    告警只走 ``call_events.emit() -> alerts.notify()``，而本模組從來沒有呼叫過
    它們。於是 docstring 承諾的「大聲失敗」只對正在 grep 容器日誌的人成立——
    這一點是承重的，因為 ``deploy_config_unverified`` 正是「正本讀不到時不擋開機」
    這個取捨的**全部**補償控制。

    不走 ``call_events.emit()`` 的理由：那個介面要求 ``room_name``，而設定層的
    事件沒有房間（開機期根本還沒有通話）。造一個假的房名去滿足簽章會讓
    ``[event] room_name=...`` 這行告警說謊。這裡直接呼叫 ``notify``，
    事件名的分流（immediate／windowed）在 ``alerts`` 那一側裁決。
    """
    fields = {"room_name": None, "field": field or None, "reason": message}
    getattr(logger.bind(call_event=event, **fields), level)(message)
    try:
        from api.services.observability import alerts

        alerts.notify(event, fields)
    except Exception as exc:  # noqa: BLE001 - 告警 MUST NOT 影響通話處理（C4）
        logger.warning(f"transfer config alert dispatch failed: {exc!r}")


#: 正本掛得到、但它對 ``queueHealthUrl`` 沒有任何 host/scheme 規則。
#:
#: **開機期是 verdict，通話期不是**（platform review gate F-1）：開機期擋下去是對的
#: ——「規則還在正本裡」MUST NOT 被讀成「控制仍生效」，而這是一個版控裡的錯誤，
#: 有人改得掉。通話期照 verdict 處置卻會把健康閘關掉，而正本**刻意**留空是一個
#: 合法狀態（CS-19／R-E 對 ``destination`` 的 ``allowed_hosts`` 就是空的）——
#: 沒有規則要執行不等於「這個值可疑」。故通話期只記一行，不停用探測。
_NO_ALLOWLIST_VERDICT = (
    "QUEUE_HEALTH_URL has no allowlist to check against: the canon carries "
    "no allowed_schemes/allowed_hosts for queueHealthUrl"
)


def _allowlist_problems(url: str) -> tuple[list[str], list[str]]:
    """``queueHealthUrl`` vs the canon's ``constrained_values`` entry.

    Returns ``(verdicts, unchecked)``: the subset of ``_check_url``'s verdicts
    that can be reached from inside this container (the canon JSON is mounted,
    its Python is not), and separately the reasons the check could not run at
    all. **The split matters after W3a §5.2**: verdicts block boot, "could not
    run" does not — a missing bind mount must not take the platform off the air
    (D-A5).

    A canon that *is* mounted but carries no rule for the key is a **verdict**,
    not "could not run": "the rule is still in the canon" MUST NOT be read as
    "the control still fires", and that one is a version-controlled mistake
    someone can fix, not a deployment-time mount slip.
    """
    from api.services.platform_scope import (
        PlatformArtifactMissing,
        log_artifact_missing,
        queue_health_url_constraints,
    )

    try:
        rule = queue_health_url_constraints()
    except PlatformArtifactMissing as exc:
        log_artifact_missing("validate_transfer_config/allowlist", exc)
        return [], [
            "QUEUE_HEALTH_URL could not be allowlist-checked: the feature scope canon is not mounted"
        ]

    schemes = rule.get("allowed_schemes")
    hosts = rule.get("allowed_hosts")
    if not schemes and not hosts:
        return [_NO_ALLOWLIST_VERDICT], []

    parts = urlsplit(url)
    problems: list[str] = []
    scheme = (parts.scheme or "").casefold()
    if schemes and scheme not in {str(x).casefold() for x in schemes}:
        problems.append(
            f"QUEUE_HEALTH_URL scheme {parts.scheme!r} is not in {list(schemes)}"
        )
    if hosts:
        # **與正本 ``_check_url`` 逐字一致的正規化**（platform review gate F-7）。
        # 正本比對的是 casefold 後、補上 scheme 預設埠的 ``hostname:port``；這裡
        # 原本比的是 raw ``netloc``（``urlsplit`` 不會小寫化它），於是
        # ``http://QUEUE:8080/…`` **通過 preflight**（正規化為 ``queue:8080``）
        # 卻**擋下 dograh 開機**——部署檢查全綠之後平台拒絕接聽電話，訊息還讀起來
        # 像真的白名單違規。差額方向是 fail-closed（不會誤放行），但兩份實作對
        # 同一組規則給出不同答案本身就是缺陷。
        #
        # userinfo 已由 ``_health_url_problem`` 的 ``@`` 檢查擋掉，所以到這裡的
        # netloc 就是 host[:port]；仍改用 ``hostname``/``port`` 重組，不倚賴那個前提。
        default_port = {"http": 80, "https": 443}.get(scheme)
        host = (parts.hostname or "").casefold()
        port = parts.port or default_port
        where = f"{host}:{port}" if port is not None else host
        allowed = {str(x).casefold() for x in hosts}
        if where not in allowed and host not in allowed:
            problems.append(f"QUEUE_HEALTH_URL host {where!r} is not in {list(hosts)}")
    return problems, []


def revalidate_transfer_config(config: dict) -> dict:
    """Merge the deployment layer in, then re-check every shape (issue #3, W3a).

    **This is also the merge point for the deployment-layer six** (W3a D3), and
    the merge happens *first*, above every check below — so validation always
    sees the **effective** value, never the definition's stale copy. The
    ordering is not a convention this function has to remember: the merge is at
    the top of the one function that is itself the validator, so "merged before
    validated" holds by position.

    **Why here and not in :func:`find_transfer_call_config`.** There are three
    readers of a transfer config, and that lookup is only on two of them:

    ==================================== =========================== ============
    reader                               trigger                     via lookup?
    ==================================== =========================== ============
    ``capacity_gate``                    capacity overflow           yes
    ``pipecat_engine``                   press-0 / safetynet         yes
    ``pipecat_engine_custom_tools``      caller asks for a human     **no**
    ==================================== =========================== ============

    The third reads ``tool.definition["config"]`` straight off the ORM row and
    calls *this* function directly (see the paragraph below, which predates
    W3a). Merging in the lookup instead would leave that path — the one its own
    comment calls "the highest-volume trigger" — with **no destination at all**
    once the version-controlled template stops carrying one, and, worse, with
    the *database* value still winning: a ``destination`` or ``queueHealthToken``
    written through the editor, or restored from an old backup, would go on
    being dialled on the busiest path while the layering claims deployment
    overrides everything.

    **Public because this lookup is not the only reader.** The AI-initiated
    transfer tool handler (``pipecat_engine_custom_tools`` /
    ``transfer_call_handler``) reads ``tool.definition["config"]`` straight off
    the ORM row — it wants *that* tool's config, not "the workflow's first
    transfer_call tool", so it cannot go through
    :func:`find_transfer_call_config` without changing behaviour on a workflow
    carrying two transfer tools. It calls this directly instead. Before W2a's
    security review found it (M-8), that path — the highest-volume trigger,
    the caller simply asking for a human — was the one reader with no
    re-validation at all.

    The write path validates these fields, but nothing re-checks them on the
    way *out*: a ``PUT /tools`` takes effect on the next call with no role
    check, and the value may also predate the current rules or have been
    written by a path that bypassed them entirely. Reading without re-checking
    makes the write-time validator the only gate, and it is not a gate that
    covers the database's existing contents.

    Field by field, because the blast radius differs:

    - **``destination`` bad → the destination is blanked, the config survives.**

      It used to return ``None``, and that was wrong in two ways at once
      (2026-08-19 review B-1 / M-5). ``None`` is indistinguishable from "this
      workflow has no transfer_call tool", and callers branch on exactly that:

        * ``resolve_press0_gate`` guards its alert on
          ``if transfer_config and not valid_destination(...)`` — with ``None``
          it fell through to a bare ``logger.info``, so a misconfigured
          deployment lost both the ``transfer.failed`` **alert dispatch** and the
          ``record_call_outcome`` annotation, and read as clean AI completions
          in the queryable layer. That alert branch exists *precisely* for this
          case; W0 added it.
        * ``capacity_gate._gate_allows`` does ``config = config or {}`` — with
          ``None``, an empty dict means "no schedule" (= always open) and
          ``queue_is_healthy({})`` returns True. One bad destination silently
          switched off **both** the business-hours gate and the queue-health gate.

      Blanking keeps the config truthy, so every existing "configured but
      malformed" path fires as designed, and ``valid_destination("")`` is False
      so nothing gets dialled.
    - **``alternateDestination`` bad → drop that key only.** It is the
      after-hours branch; killing the main transfer path over it would trade a
      degraded branch for a dead one.
    - **``queueHealthUrl`` bad → drop the health keys only.** The transfer then
      proceeds without a health probe, which is the documented pre-S-L5-QUEUE
      behaviour.

    Every drop is logged at high signal. Nothing here is silent — that is the
    whole point of the task (MUST NOT silently use).
    """
    from api.services.platform_scope import (
        PlatformArtifactMissing,
        log_artifact_missing,
        parse_refer_uri,
    )

    # Deployment layer first (W3a D3) — everything below validates the merged,
    # effective value. "It came from the deployment env" is **not** a licence to
    # skip the shape gate or the premium-rate guard: that is this change's
    # single most likely failure mode, so the merge deliberately lands above
    # the checks rather than beside them.
    config = _merge_deployment_layer(config)

    destination = config.get("destination")
    try:
        parsed = parse_refer_uri(destination)
    except PlatformArtifactMissing as exc:
        # Fail closed, matching the call-time tool filter: with the parser gone
        # we cannot tell a queue from an attacker's SIP host, and the two
        # artifacts are mounted together, so the tool itself is about to be
        # dropped by the enabled-set filter anyway.
        log_artifact_missing("revalidate_transfer_config", exc)
        _config_event(
            "transfer.config_unvalidatable",
            "transfer.config_unvalidatable: shared REFER URI parser unavailable; "
            "blanking the destination (fail-closed, W2a)",
            field="destination",
        )
        return dict(config, destination="")

    if not parsed.ok:
        _config_event(
            "transfer.config_rejected",
            f"transfer.config_rejected field=destination: {parsed.reason}; "
            f"destination blanked — the configured-but-malformed path takes over "
            f"(W2a issue #3)",
            field="destination",
        )
        return dict(config, destination="")

    # Premium-rate guard (2026-08-19 review M-1). The write path runs shape
    # **and** premium-rate; the read path ran only shape — so a `tel:+1900…`
    # sitting in the database was shape-perfect and dialled every time. The
    # read path exists precisely because the database's contents never went
    # through the write path.
    from api.services.pipecat.capacity_gate import PREMIUM_RATE_PREFIXES, _premium_rate

    if _premium_rate(destination):
        _config_event(
            "transfer.config_rejected",
            f"transfer.config_rejected field=destination: matches a premium-rate "
            f"prefix {PREMIUM_RATE_PREFIXES}; destination blanked (review M-1)",
            field="destination",
        )
        return dict(config, destination="")

    checked = dict(config)

    alternate = checked.get("alternateDestination")
    if alternate is not None and str(alternate).strip():
        alt_parsed = parse_refer_uri(alternate)
        if alt_parsed.ok and _premium_rate(alternate):
            _config_event(
                "transfer.config_rejected",
                "transfer.config_rejected field=alternateDestination: premium-rate "
                "prefix; after-hours alternate branch disabled (review M-1)",
                field="alternateDestination",
            )
            checked.pop("alternateDestination", None)
        elif not alt_parsed.ok:
            _config_event(
                "transfer.config_rejected",
                f"transfer.config_rejected field=alternateDestination: "
                f"{alt_parsed.reason}; after-hours alternate branch disabled for "
                f"this call (W2a issue #3)",
                field="alternateDestination",
            )
            checked.pop("alternateDestination", None)

    health_url = checked.get("queueHealthUrl")
    if health_url is not None and str(health_url).strip():
        problem = _health_url_problem(str(health_url))
        if problem:
            _config_event(
                "transfer.config_rejected",
                f"transfer.config_rejected field=queueHealthUrl: {problem}; "
                f"queue health probe disabled for this call (W2a issue #3)",
                field="queueHealthUrl",
            )
            for key in ("queueHealthUrl", "queueHealthToken"):
                checked.pop(key, None)
        else:
            # **白名單也要有通話期執行點**（platform review gate F-1／H3）。
            # 在此之前它只有兩個執行點：preflight §7（部署期一次性，有已知繞道）
            # 與開機期步驟③——而③在正本讀不到時降級為 ``unverifiable``、**不擋開機**
            # （D-A5 的取捨，本身是對的）。於是「掛載存在但檔案讀不動／半寫入／
            # 編碼壞掉」這幾種狀態下，一個被改壞的 ``QUEUE_HEALTH_URL`` 會讓每一次
            # 健康探測把 ``Authorization: Bearer <QUEUE_HEALTH_TOKEN>`` 送去該主機。
            # egress 圍堵把可達面縮到內網（``FilterDefaultDeny`` ＋ internal network），
            # 但那是**另一道控制**，不是這一格自己的。
            #
            # **「檢查跑不成」與「值不合格」在這裡採同一個處置**，理由與開機期相反：
            # 開機期擋下去等於平台停止接聽電話（不可接受）；通話期只是**停用探測**，
            # 而那是 ``queue_health`` 已經明文當成可接受降級的處置（未設定即
            # ``return True``）。代價是失去健康閘，收益是憑證不外送到未驗證的主機——
            # 前者有 C4 的其他出口兜著，後者沒有。
            verdicts, unchecked = _allowlist_problems(str(health_url))
            # 「正本沒有規則」不在通話期停用探測——見 _NO_ALLOWLIST_VERDICT。
            no_rule = [v for v in verdicts if v == _NO_ALLOWLIST_VERDICT]
            verdicts = [v for v in verdicts if v != _NO_ALLOWLIST_VERDICT]
            if no_rule and not (verdicts or unchecked):
                _config_event(
                    "transfer.deploy_config_unverified",
                    "transfer.deploy_config_unverified field=queueHealthUrl: "
                    + _NO_ALLOWLIST_VERDICT
                    + "; probe continues (an empty allowlist is a legal canon state)",
                    field="queueHealthUrl",
                    level="warning",
                )
            if verdicts or unchecked:
                reason = "; ".join(verdicts or unchecked)
                event = (
                    "transfer.config_rejected"
                    if verdicts
                    else "transfer.deploy_config_unverified"
                )
                _config_event(
                    event,
                    f"{event} field=queueHealthUrl: {reason}; queue health probe "
                    f"disabled for this call and the bearer token is NOT sent "
                    f"(review gate F-1)",
                    field="queueHealthUrl",
                )
                for key in ("queueHealthUrl", "queueHealthToken"):
                    checked.pop(key, None)

    return checked


def _deployment_only_config(why: str) -> dict | None:
    """兩個早退點的共用處置（platform review gate F-11）。

    **問題**：這兩個 ``return None`` 都在 merge **之前**。持編輯器寫入權者把
    ``transfer_call`` 自節點圖的 ``tool_uuids`` 移除——那是一次不宣告受管型別的
    ``workflow_definition`` 寫入，依 ``workflow-editor-access`` 的判準
    **admission 正常放行**——於是：

    - ``capacity_gate._gate_allows`` 走 ``config or {}`` → ``queue_is_healthy({})``
      在 ``queue_health`` 的 ``if not url: return True`` **fail-open** ⇒ 滿線溢流把
      每一位被拒的來電者 REFER 進一個可能已死的隊列，且排程閘同時失效（恆「營業中」）；
    - ``press0_gate`` 走安靜分支（``transfer.failed`` 的守衛是
      ``if transfer_config and ...``，而它是 ``None``），press-0 只留一行 info 就消失。

    開機期驗證與 preflight 都是綠的——它們驗的是 env，而這條路徑根本走不到讀 env
    的那個函式。「部署層覆蓋一切」被一次 DB 寫入**從下游繞開**。

    **判準**：部署層有沒有宣告目的地。有 ⇒ 這個平台的意圖就是「轉真人」，
    而節點圖沒有轉接工具是**缺陷**（引用掉了），不是工作流的選擇 ⇒ 回傳部署層
    自己就足以支撐兩張安全網的設定，並大聲說出來。沒有 ⇒ ``None`` 仍然正確，
    press-0 的安靜分支（「這個工作流本來就不轉真人」）原封不動。

    話術層會缺席，那是這個降級的已知代價：來電者聽到的是**內建預設**而不是這套
    部署客製的字。相對於 fail-open 地 REFER 進死隊列、或整條轉真人路徑無聲消失，
    少一句客製話術是可接受的那一邊。

    **更正（W3a §9.3 security F-17 複驗）**：本段原本寫「``transferFailedMessage``
    有內建預設，``transferUnavailableMessage`` 沒有」——後半不成立。
    ``_announce_unavailable`` 播的是 ``message or _DEFAULT_UNAVAILABLE_MESSAGE``
    （``livekit_transfer_flow``），四個話術層執行點**都有**碼層預設：
    ``transferFailedMessage``→``press0_gate._DEFAULT_FAILURE_MESSAGE``、
    ``transferUnavailableMessage``／``afterHoursMessage``→
    ``livekit_transfer_flow`` 的兩個 ``_DEFAULT_*_MESSAGE``、
    ``unavailableAnnounceLimit``→``DEFAULT_UNAVAILABLE_ANNOUNCE_LIMIT = 2``。
    所以少任何一鍵都不會產生無聲掛斷或無上限迴圈（C4 兩條出口都還在），
    ``test_every_c4_exit_has_a_code_level_default`` 把這件事釘住。
    ``feature-scope.json`` 的 ``required_keys`` 只列兩鍵**不是** C4 的漏洞：
    它防的是「一次回送不全的寫入把營運者設定的字刪掉」，不是防無聲。
    """
    supplied = deployment_transfer_config()
    if not str(supplied.get("destination") or "").strip():
        return None
    _config_event(
        "transfer.failed",
        f"transfer.failed field=tool_uuids: {why}, but the deployment layer "
        f"declares a transfer destination; falling back to the deployment-layer "
        f"config so press-0 and capacity overflow keep a route to a human "
        f"(review gate F-11). Prompt-layer messages are unavailable for this call.",
        field="tool_uuids",
    )
    return revalidate_transfer_config({})


async def find_transfer_call_config(workflow, organization_id: int) -> dict | None:
    """Return the workflow's ``transfer_call`` tool config, or None if absent.

    Scans every node's tools (a press-0 safety net is global, so the target is
    workflow-wide, not per-node) and returns the first ``transfer_call`` tool's
    ``config`` — **re-validated**, see :func:`revalidate_transfer_config`.

    This is a ``get_tools_by_uuids`` path that deliberately does **not** consult
    the enabled set, unlike the three in ``pipecat_engine``/
    ``pipecat_engine_custom_tools`` (review B-3). Those three decide what the
    LLM may call; this one feeds press-0 and capacity overflow, which are
    platform safety nets the caller reaches without the LLM. Gating it on the
    canon would mean an unreadable bind mount silently removes the route to a
    human — fail-closed in the wrong direction for C4. What it does instead is
    re-validate the value, which is the check that actually matters here.
    """
    tool_uuids: set[str] = set()
    for node in workflow.nodes.values():
        for tu in getattr(node, "tool_uuids", None) or []:
            tool_uuids.add(tu)
    if not tool_uuids:
        return _deployment_only_config("the node graph declares no tool_uuids")

    tools = await db_client.get_tools_by_uuids(list(tool_uuids), organization_id)
    transfer_tools = [
        tool for tool in tools if tool.category == ToolCategory.TRANSFER_CALL.value
    ]
    if not transfer_tools:
        return _deployment_only_config(
            "no transfer_call tool among the node graph's tool_uuids"
        )

    if len(transfer_tools) > 1:
        # Deterministic by construction (get_tools_by_uuids orders by id), but the
        # choice is still arbitrary: the workflow declares two transfer targets and
        # only one of them is reachable. Not an error — raising here would remove
        # the route to a human, which is the wrong direction for C4 — so pick and
        # say so loudly enough to be found when the call went to the wrong queue.
        logger.warning(
            "workflow declares {} active transfer_call tools; using {} (tool_uuids={})",
            len(transfer_tools),
            transfer_tools[0].tool_uuid,
            [tool.tool_uuid for tool in transfer_tools],
        )

    return revalidate_transfer_config(
        (transfer_tools[0].definition or {}).get("config", {}) or {}
    )
