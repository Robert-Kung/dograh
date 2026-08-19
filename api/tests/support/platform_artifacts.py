"""Locating the platform repo's artifacts from inside the test suite (W2a).

Two files this fork depends on at runtime live in the *platform* repo
(``customer-center-platform/deploy/``) and reach the container as read-only
bind mounts. Neither is part of this repository, so their availability differs
by where the suite runs:

- **Platform environment** (``dograh-test`` sidecar, the deployed stack): both
  are present at ``/opt/platform/``; everything below runs for real.
- **This fork's own CI** (``api-tests.yml``): the platform repo is not checked
  out, so ``sip_uri.py`` does not exist. Tests that need the real parser skip
  with an explicit reason rather than being deleted or given a second copy of
  the rule — a vendored copy is precisely the drift W2a exists to remove.

That asymmetry is a known cost of the submodule boundary (residual R-J). The
authoritative run for these tests is the platform repo's, not this one's.

The enabled set is different in kind: it is *deployment policy*, not a rule.
Tests therefore declare which policy they run under. The default is
:data:`PERMISSIVE_SCOPE` (everything allowed = upstream behaviour), so the
pre-existing suite keeps testing upstream features; the tests that care about
the filter point ``PLATFORM_FEATURE_SCOPE`` at a restrictive canon themselves.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

SUPPORT_DIR = Path(__file__).resolve().parent

#: Test-only canon allowing every ToolCategory. **Not** the delivered canon —
#: that one lives in ``deploy/feature-scope.json`` and allows two categories.
PERMISSIVE_SCOPE = SUPPORT_DIR / "feature_scope_permissive.json"

#: Test-only canon matching what the platform actually ships.
DELIVERED_SCOPE = SUPPORT_DIR / "feature_scope_delivered_shape.json"

DEFAULT_SIP_URI = Path("/opt/platform/sip_uri.py")


def sip_uri_path() -> Path:
    return Path(os.environ.get("PLATFORM_SIP_URI") or DEFAULT_SIP_URI)


def sip_uri_available() -> bool:
    return sip_uri_path().is_file()


#: Module-level marker for tests that exercise the real shared parser.
requires_sip_uri = pytest.mark.skipif(
    not sip_uri_available(),
    reason=(
        "shared REFER URI parser not present. It lives in the platform repo "
        "(deploy/bin/sip_uri.py) and is bind-mounted into the api container; "
        "point PLATFORM_SIP_URI at it, or run this suite from the platform "
        "repo's dograh-test sidecar. See W2a / residual R-J."
    ),
)
