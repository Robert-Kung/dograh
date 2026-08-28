"""Runtime access to the platform repo's canonical artifacts (W2a D-A1/D-A3/D-A5).

Two files live in the *platform* repo (``customer-center-platform/deploy/``) and
are bind-mounted read-only into this container:

===============================  ===================================================
``PLATFORM_SIP_URI``             ``deploy/bin/sip_uri.py`` — the single REFER
                                 destination parser. Replaces three divergent
                                 shape rules (this repo had two of them).
``PLATFORM_FEATURE_SCOPE``       ``deploy/feature-scope.json`` — the enabled-set
                                 canon. ``allowed_tool_types`` holds **dispatch
                                 keys** (``ToolCategory.value``), not the
                                 version-controlled ``definition.type`` proxy.
===============================  ===================================================

**Everything here is lazy.** Nothing in this module may be imported at the
module level of a schema or a service: a missing bind mount would then mean
*dograh-api does not start* — i.e. the platform stops answering the phone
entirely — rather than one feature degrading (D-A5). Import this module freely;
just never call into it at import time.

The failure shapes are deliberately **not** uniform, because the safe direction
differs per call site. Each caller documents which one it picked:

- **Write paths** (``TransferCallConfig.validate_destination``) fail *closed*:
  the field is rejected. Refusing a write is safe; accepting an unvalidated
  destination is not.
- **Call-time registration** (``pipecat_engine_custom_tools``) fails *closed*:
  no governed tool is registered. The call itself continues.
- **The premium-rate guard** (``capacity_gate._premium_rate``) falls back to a
  parser-free scan of every ``@``-separated part, with a high-signal log. It is
  a *guard*, so the fallback errs towards over-blocking and is strictly not
  weaker than the pre-W2a check, whereas raising would take a boot-time config
  check down and with it the whole API.

The missing-mount case is caught two gates earlier in normal operation
(``preflight.sh`` §7/§8b and ``dograh-bootstrap.py`` both fail closed on it).
What is left for these paths is "container already up, compose hand-edited" —
residual R-O.

**Running this repo's bare ``docker-compose.yaml`` is a different thing, and it
is already forbidden.** That file mounts neither artifact and sets neither env
var, so a stack brought up that way registers no governed tool and refuses
every transfer-tool write. Codex review (2026-08-20) flagged this as a gap and
asked for a local fallback for standalone installs; **declined, deliberately.**
The platform stack is brought up by ``deploy/platform-up.sh``, which layers
``deploy/overrides/dograh.override.yml`` (the mounts and both env vars live
there) — and the RUNBOOK already bans bare ``docker compose up`` in this
directory because it bypasses *every* hardening in that override, not just
these two files. A fallback would have to fire on "neither env var nor default
path", which is indistinguishable from the R-O state this module exists to fail
closed on; buying convenience for a banned path by weakening the one that ships
is the wrong trade. Contributors doing local dev per ``AGENTS.md`` hit the
``scope.canon_unavailable`` log line, which names the path and the missing
``-v``. Registered as residual R-AA in the platform repo (not R-T — R-T…R-Z were
pre-allocated to W2c/W2d and the point of pre-allocation is that a late arrival
must not shift them).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_SIP_URI_PATH = "/opt/platform/sip_uri.py"
DEFAULT_FEATURE_SCOPE_PATH = "/opt/platform/feature-scope.json"

_MODULE_NAME = "platform_sip_uri"


class PlatformArtifactMissing(RuntimeError):
    """A platform artifact this container depends on is not readable.

    Carries the resolved path so the operator learns *which ``-v`` is missing*
    — ``ModuleNotFoundError`` does not say that.
    """


# Import/parse results are cached: both files are read-only bind mounts, and
# the call-time filter runs per tool per call. ``reset_cache`` exists for tests
# only — nothing in the runtime rereads.
_cache: dict[str, Any] = {}


def reset_cache() -> None:
    """Drop memoized artifacts. Tests only."""
    _cache.clear()


def sip_uri_path() -> Path:
    return Path(os.environ.get("PLATFORM_SIP_URI") or DEFAULT_SIP_URI_PATH)


def feature_scope_path() -> Path:
    return Path(os.environ.get("PLATFORM_FEATURE_SCOPE") or DEFAULT_FEATURE_SCOPE_PATH)


def load_sip_uri():
    """Import ``sip_uri`` from the bind mount. Raises PlatformArtifactMissing."""
    cached = _cache.get("sip_uri")
    if cached is not None:
        return cached

    path = sip_uri_path()
    if not path.is_file():
        raise PlatformArtifactMissing(
            f"shared REFER URI parser not readable at {path}; the api container "
            "needs deploy/bin/sip_uri.py bind-mounted read-only (see "
            "deploy/overrides/dograh.override.yml) and PLATFORM_SIP_URI pointing "
            "at it"
        )
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise PlatformArtifactMissing(f"cannot load a module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's own ``from __future__``/dataclass
    # machinery resolves normally, mirroring a real import.
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - corrupt mount
        sys.modules.pop(_MODULE_NAME, None)
        raise PlatformArtifactMissing(
            f"shared REFER URI parser at {path} failed to load: {type(exc).__name__}"
        ) from exc
    _cache["sip_uri"] = module
    return module


def parse_refer_uri(value):
    """``sip_uri.parse_refer_uri``. Raises PlatformArtifactMissing if unmounted.

    The result never raises on bad input — ``result.ok`` is False and
    ``result.reason`` is a fixed message that **contains no part of the input**
    (the value here can be a customer number or an internal PBX host, and both
    callers log it).
    """
    return load_sip_uri().parse_refer_uri(value)


def load_feature_scope() -> dict:
    """Parse the enabled-set canon. Raises PlatformArtifactMissing."""
    cached = _cache.get("feature_scope")
    if cached is not None:
        return cached

    path = feature_scope_path()
    if not path.is_file():
        raise PlatformArtifactMissing(
            f"feature scope canon not readable at {path}; the api container needs "
            "deploy/feature-scope.json bind-mounted read-only (see "
            "deploy/overrides/dograh.override.yml) and PLATFORM_FEATURE_SCOPE "
            "pointing at it"
        )
    try:
        scope = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PlatformArtifactMissing(
            f"feature scope canon at {path} is not parseable JSON: {type(exc).__name__}"
        ) from exc
    if not isinstance(scope, dict):
        raise PlatformArtifactMissing(f"feature scope canon at {path} is not an object")
    _cache["feature_scope"] = scope
    return scope


def allowed_tool_categories() -> frozenset[str]:
    """The enabled set, as **dispatch keys** (``ToolCategory.value``).

    ``allowed_tool_types`` in the canon is documented as comparing against the
    runtime dispatch key; ``definition.type`` in a version-controlled workflow
    is only its proxy and the two can diverge permanently (``UpdateToolRequest``
    carries no category, so a PUT never re-derives it). Registration looks at
    ``tool.category`` alone — so does this.

    An empty or absent list is an error, not "allow nothing by accident": the
    canon always names at least ``end_call``. Callers treat the raise as
    fail-closed.
    """
    scope = load_feature_scope()
    allowed = scope.get("allowed_tool_types")
    if not isinstance(allowed, list) or not allowed:
        raise PlatformArtifactMissing(
            f"feature scope canon at {feature_scope_path()} has no usable "
            "allowed_tool_types list"
        )
    return frozenset(str(item) for item in allowed)


def queue_health_url_constraints() -> dict:
    """``field_rules.constrained_values.queueHealthUrl`` from the canon.

    W3a D10/D11: the deployment-layer six moved out of ``definition.config``,
    so ``feature_scope_check.check_definition`` no longer has a ``queueHealthUrl``
    key to match — the ``allowed_hosts`` rule (the **only** egress destination
    allowlist that actually fires anywhere in this system, CS-19/R-E) would
    become dead code with no alarm. The platform repo re-points it at the
    deployment env in ``preflight.sh``; this accessor is the *boot-time* half,
    so the value the app will actually dial is checked against the same canon
    rather than against a second hand-maintained copy inside this repo.

    ``_check_url`` itself (the richer implementation: userinfo, IDN, trailing
    dot, explicit ports) lives in ``deploy/bin/feature_scope_check.py`` and is
    **not** mounted here — only the JSON is. So this returns the *rule*, and
    :func:`~api.services.pipecat.transfer_call_config.validate_transfer_config`
    applies the subset it can: scheme and ``host:port``. The difference is a
    declared delta, not an oversight.

    Returns ``{}`` when the canon carries no rule for the key — the caller
    treats that as "no allowlist to enforce" and says so, rather than inventing
    one.
    """
    scope = load_feature_scope()
    rules = scope.get("field_rules")
    if not isinstance(rules, dict):
        return {}
    constrained = rules.get("constrained_values")
    if not isinstance(constrained, dict):
        return {}
    entry = constrained.get("queueHealthUrl")
    return entry if isinstance(entry, dict) else {}


def log_artifact_missing(where: str, exc: PlatformArtifactMissing) -> None:
    """One high-signal line, shaped like ``tool_trust.log_denied_tool``.

    Kept here so the wording is identical wherever it fires — this line is what
    an operator greps for when the phone behaves oddly after a compose edit.
    """
    logger.error(f"platform artifact unavailable at {where}: {exc}")
