"""The 1.4 failure exits are wired into the pipeline as one (5.1 review R4).

``_run_pipeline_impl`` hands one per-call ``CallFailureState`` to both the
safetynet watchdog and the LLM gates, registers the STT's ``on_connected`` on
the watchdog, and passes the watchdog to ``register_event_handlers`` (without
it, ``on_pipeline_error`` cancels the call on every ErrorFrame again). None of
that was under test: every test built the watchdog, the state and the gates
itself, and the tests that reach ``run_pipeline_livekit`` replace it whole.

The wiring now lives in ``LiveKitFailureExits`` (behavior tested here against
the real watchdog), and the last two hand-offs that stay in
``_run_pipeline_impl`` are pinned by reading its source: the gates get
``failure_exits.state`` and the event handlers get ``failure_exits.watchdog``.
"""

import ast
import inspect
import textwrap
import types

import pytest

from api.enums import WorkflowRunMode
from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat import run_pipeline
from api.services.pipecat.livekit_failure_exits import CallFailureState
from api.services.pipecat.livekit_safetynet import (
    LiveKitFailureExits,
    SafetynetWatchdog,
)

pytestmark = pytest.mark.usefixtures("reset_safetynet_latch")


class FakeSTT:
    """Keeps the event handlers registered on it, like a pipecat service."""

    def __init__(self):
        self.handlers = {}

    def event_handler(self, name):
        def register(handler):
            self.handlers.setdefault(name, []).append(handler)
            return handler

        return register


class FakeTask:
    def __init__(self):
        self.observers = []

    def add_observer(self, observer):
        self.observers.append(observer)


def _run(mode=WorkflowRunMode.LIVEKIT.value, room_name="cs-+886912345678"):
    return types.SimpleNamespace(
        id=42,
        mode=mode,
        initial_context={"room_name": room_name} if room_name else {},
    )


@pytest.fixture
def quiet_install(monkeypatch):
    """The not-installed branch reports to the event sink and the DB."""
    from api.services.observability import call_events

    monkeypatch.setattr(call_events, "emit", lambda event, **fields: None)

    async def fake_fact(workflow_run_id, **facts):
        pass

    monkeypatch.setattr(sn, "record_call_fact", fake_fact)


async def test_cascaded_livekit_call_shares_one_state():
    """Watchdog, STT stop and gates all act on the same state: the gates close
    and the STT stops when the watchdog's exit says so."""
    stt, task = FakeSTT(), FakeTask()
    exits = LiveKitFailureExits(_run(), stt, is_realtime=False)

    assert isinstance(exits.state, CallFailureState)
    assert exits.state._stt is stt

    watchdog = await exits.install_watchdog(object(), task)

    assert isinstance(watchdog, SafetynetWatchdog)
    assert exits.watchdog is watchdog
    assert watchdog.state is exits.state
    assert task.observers == [watchdog]
    await watchdog.stop()


async def test_stt_connections_reach_the_watchdog():
    """A stable STT connection resets the error count (design D9, STT row)."""
    stt, task = FakeSTT(), FakeTask()
    exits = LiveKitFailureExits(_run(), stt, is_realtime=False)
    watchdog = await exits.install_watchdog(object(), task)
    noted = []
    watchdog.note_stt_connected = lambda: noted.append("connected")

    [handler] = stt.handlers["on_connected"]
    await handler(stt)

    assert noted == ["connected"]
    await watchdog.stop()


async def test_realtime_livekit_call_has_no_shared_state():
    """Realtime pipelines have no STT to stop and no gate slot: the watchdog
    keeps a state of its own."""
    task = FakeTask()
    exits = LiveKitFailureExits(_run(), None, is_realtime=True)

    assert exits.state is None
    watchdog = await exits.install_watchdog(object(), task)

    assert isinstance(watchdog.state, CallFailureState)
    assert task.observers == [watchdog]
    await watchdog.stop()


async def test_non_livekit_call_gets_no_exits():
    stt, task = FakeSTT(), FakeTask()
    exits = LiveKitFailureExits(
        _run(mode=WorkflowRunMode.WEBRTC.value), stt, is_realtime=False
    )

    assert exits.state is None
    assert await exits.install_watchdog(object(), task) is None
    assert exits.watchdog is None
    assert task.observers == []
    assert stt.handlers == {}


async def test_watchdog_not_installed_leaves_nothing_half_wired(quiet_install):
    stt, task = FakeSTT(), FakeTask()
    exits = LiveKitFailureExits(_run(room_name=None), stt, is_realtime=False)

    assert await exits.install_watchdog(object(), task) is None
    assert exits.watchdog is None
    assert task.observers == []
    assert stt.handlers == {}


# --- the hand-offs that stay in _run_pipeline_impl ---------------------------


def _impl_calls():
    """Calls made in ``_run_pipeline_impl``, keyed by the called name."""
    source = textwrap.dedent(inspect.getsource(run_pipeline._run_pipeline_impl))
    calls = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            calls.setdefault(name, []).append(node)
    return calls


def _kwarg(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.unparse(keyword.value)
    return None


def test_pipeline_gates_get_the_failure_exits_state():
    [build] = _impl_calls()["build_pipeline"]

    assert _kwarg(build, "failure_state") == "failure_exits.state"


def test_event_handlers_get_the_failure_exits_watchdog():
    """Without it ``on_pipeline_error`` cancels the call on any ErrorFrame."""
    [register] = _impl_calls()["register_event_handlers"]

    assert _kwarg(register, "safetynet_watchdog") == "failure_exits.watchdog"


def test_failure_exits_are_built_once_and_installed():
    calls = _impl_calls()

    [build] = calls["LiveKitFailureExits"]
    assert _kwarg(build, "is_realtime") == "is_realtime"
    assert len(calls["install_watchdog"]) == 1
    # Nothing builds a second state or watchdog beside the shared one.
    assert "CallFailureState" not in calls
    assert "resolve_safetynet_watchdog" not in calls
