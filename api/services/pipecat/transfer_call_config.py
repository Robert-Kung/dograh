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

def deployment_transfer_config() -> dict:
    """部署層供給的轉接設定，只含**實際供給**的鍵。缺值不入結果、不拋例外。

    **每次呼叫都讀 ``os.environ``**（D2）。MUST NOT 在 import 期讀進模組常數：
    D5 宣稱「憑證輪替只要改 env 就生效、不需要 re-apply」，而 import 期讀會讓輪替
    需要重啟 ``dograh-api``，那會斷掉進行中的通話——該宣稱就不成立了。

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
# **這個數字在 ``deploy/preflight.sh`` 有一份刻意的複本**（W3a §2.7）：那一份是
# 部署期的擋門，這一份是開機期的。兩處都要，理由與 D10 相同——preflight 有已知
# 繞道且是一次性，而本檔看得到行程實際讀到的值。改一處 SHALL 同批改另一處。
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
        from api.services.platform_scope import (
            PlatformArtifactMissing,
            log_artifact_missing,
            parse_refer_uri,
        )

        try:
            for key, env_name, value in destinations:
                parsed = parse_refer_uri(value)
                if not parsed.ok:
                    # ``parsed.reason`` 依該模組契約不含輸入的任何片段——這個值
                    # 可能是客戶號碼或內部 PBX 主機，而本訊息會進啟動日誌。
                    problems.append(
                        f"{env_name} is not a valid REFER target: {parsed.reason}"
                    )
                    continue
                from api.services.pipecat.capacity_gate import (
                    PREMIUM_RATE_PREFIXES,
                    _premium_rate,
                )

                if _premium_rate(value):
                    problems.append(
                        f"{env_name} matches a premium-rate prefix {PREMIUM_RATE_PREFIXES}"
                    )
        except PlatformArtifactMissing as exc:
            # 缺 mount 不擋開機（D-A5 的既有取捨：一個少掉的 ``-v`` MUST NOT 變成
            # 「dograh-api 不啟動」＝平台停止接聽電話）。記為一條 problem，讓它照
            # 本函式的模式處置。
            log_artifact_missing("validate_transfer_config", exc)
            unverifiable.append(
                "transfer destinations could not be shape-checked: the shared "
                "REFER URI parser is not mounted"
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
        if value < _MIN_PROBE_SECONDS:
            problems.append(
                f"{env_name} is {value}, below the {_MIN_PROBE_SECONDS}s floor; "
                "a probe budget this small times out every time, which pins the "
                "health verdict to unhealthy and refuses every in-hours transfer"
            )

    # 「沒驗成」永遠說出來，**且在拋例外之前說**：不合格與沒驗成可能同時發生，
    # 而 RuntimeError 只帶得走前者。先 log 才不會讓後者被前者吃掉。
    for item in unverifiable:
        logger.bind(call_event="transfer.deploy_config_unverified").error(
            f"transfer.deploy_config_unverified: {item} "
            "(boot continues by design — a missing bind mount must not take the "
            "platform off the air; this value was NOT checked at boot)"
        )

    if not problems:
        return

    raise RuntimeError(
        "transfer deployment config is not usable: " + "; ".join(problems)
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
        return [
            "QUEUE_HEALTH_URL has no allowlist to check against: the canon carries "
            "no allowed_schemes/allowed_hosts for queueHealthUrl"
        ], []

    parts = urlsplit(url)
    problems: list[str] = []
    if schemes and parts.scheme not in schemes:
        problems.append(
            f"QUEUE_HEALTH_URL scheme {parts.scheme!r} is not in {list(schemes)}"
        )
    if hosts:
        # ``netloc`` 而非 ``hostname``：正本的 allowed_hosts 逐字是 ``queue:8080``，
        # 埠是它的一部分。userinfo 已由 _health_url_problem 的 ``@`` 檢查擋掉，
        # 故此處的 netloc 就是 host[:port]。
        if parts.netloc not in hosts:
            problems.append(
                f"QUEUE_HEALTH_URL host {parts.netloc!r} is not in {list(hosts)}"
            )
    return problems, []


def revalidate_transfer_config(config: dict) -> dict | None:
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
        logger.bind(call_event="transfer.config_unvalidatable").error(
            "transfer.config_unvalidatable: shared REFER URI parser unavailable; "
            "blanking the destination (fail-closed, W2a)"
        )
        return dict(config, destination="")

    if not parsed.ok:
        logger.bind(call_event="transfer.config_rejected").error(
            f"transfer.config_rejected field=destination: {parsed.reason}; "
            f"destination blanked — the configured-but-malformed path takes over "
            f"(W2a issue #3)"
        )
        return dict(config, destination="")

    # Premium-rate guard (2026-08-19 review M-1). The write path runs shape
    # **and** premium-rate; the read path ran only shape — so a `tel:+1900…`
    # sitting in the database was shape-perfect and dialled every time. The
    # read path exists precisely because the database's contents never went
    # through the write path.
    from api.services.pipecat.capacity_gate import PREMIUM_RATE_PREFIXES, _premium_rate

    if _premium_rate(destination):
        logger.bind(call_event="transfer.config_rejected").error(
            f"transfer.config_rejected field=destination: matches a premium-rate "
            f"prefix {PREMIUM_RATE_PREFIXES}; destination blanked (review M-1)"
        )
        return dict(config, destination="")

    checked = dict(config)

    alternate = checked.get("alternateDestination")
    if alternate is not None and str(alternate).strip():
        alt_parsed = parse_refer_uri(alternate)
        if alt_parsed.ok and _premium_rate(alternate):
            logger.bind(call_event="transfer.config_rejected").error(
                "transfer.config_rejected field=alternateDestination: premium-rate "
                "prefix; after-hours alternate branch disabled (review M-1)"
            )
            checked.pop("alternateDestination", None)
        elif not alt_parsed.ok:
            logger.bind(call_event="transfer.config_rejected").error(
                f"transfer.config_rejected field=alternateDestination: "
                f"{alt_parsed.reason}; after-hours alternate branch disabled for "
                f"this call (W2a issue #3)"
            )
            checked.pop("alternateDestination", None)

    health_url = checked.get("queueHealthUrl")
    if health_url is not None and str(health_url).strip():
        problem = _health_url_problem(str(health_url))
        if problem:
            logger.bind(call_event="transfer.config_rejected").error(
                f"transfer.config_rejected field=queueHealthUrl: {problem}; "
                f"queue health probe disabled for this call (W2a issue #3)"
            )
            for key in ("queueHealthUrl", "queueHealthToken"):
                checked.pop(key, None)

    return checked


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
        return None

    tools = await db_client.get_tools_by_uuids(list(tool_uuids), organization_id)
    transfer_tools = [
        tool for tool in tools if tool.category == ToolCategory.TRANSFER_CALL.value
    ]
    if not transfer_tools:
        return None

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
