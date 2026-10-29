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


def _impl_tree():
    source = textwrap.dedent(inspect.getsource(run_pipeline._run_pipeline_impl))
    return ast.parse(source)


def _impl_calls(tree=None):
    """Calls made in ``_run_pipeline_impl``, keyed by the called name."""
    calls = {}
    for node in ast.walk(tree or _impl_tree()):
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


def test_watchdog_is_installed_before_the_event_handlers_get_it():
    """5.1 fix review R5: ``failure_exits.watchdog`` is None until
    ``install_watchdog`` has completed, so it must be awaited, and before
    ``register_event_handlers`` reads it."""
    tree = _impl_tree()
    awaited = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Await)}
    calls = _impl_calls(tree)
    [install] = calls["install_watchdog"]
    [register] = calls["register_event_handlers"]

    assert id(install) in awaited
    assert install.lineno < register.lineno


def _livekit_call_flag(tree):
    """The name ``_run_pipeline_impl`` binds to "this run is LIVEKIT"."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Compare)
            and "WorkflowRunMode.LIVEKIT" in ast.unparse(node.value)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            yield node.targets[0].id


def test_livekit_only_settings_follow_the_run_mode():
    """The TTS stream timeout (5.1 fix review R4) and the shorter idle timeout
    (recheck N1) are both keyed on the run's LIVEKIT mode — the calls whose
    safetynet turns a timeout into a transfer and whose idle path has the
    20 s silence budget. Elsewhere ``on_pipeline_error`` would end the call on
    a TTS timeout. The defaults per mode are tested on
    ``resolve_max_user_idle_timeout`` itself; this pins that both settings get
    the same mode flag."""
    tree = _impl_tree()
    flags = set(_livekit_call_flag(tree))
    calls = _impl_calls(tree)
    [create_tts] = calls["create_tts_service"]
    [resolve_idle] = calls["resolve_max_user_idle_timeout"]

    assert _kwarg(create_tts, "livekit_call") in flags
    assert _kwarg(resolve_idle, "livekit_call") == _kwarg(create_tts, "livekit_call")


@pytest.mark.parametrize(
    "run_configs, livekit_call, expected",
    [
        ({}, True, 8.0),
        ({}, False, 10.0),
        (None, True, 8.0),
        ({"max_user_idle_timeout": 15}, True, 15),
        ({"max_user_idle_timeout": 15}, False, 15),
    ],
    ids=[
        "livekit-default",
        "other-modes-default",
        "livekit-no-configs",
        "livekit-workflow-setting",
        "other-modes-workflow-setting",
    ],
)
def test_idle_timeout_defaults_by_mode_and_honors_the_workflow(
    run_configs, livekit_call, expected
):
    """recheck N1: LIVEKIT calls wait 8 s, not 10, before the idle prompt so
    the idle turn's bot_silence exit fits the 20 s budget; a workflow's own
    ``max_user_idle_timeout`` is still honored."""
    assert (
        run_pipeline.resolve_max_user_idle_timeout(
            run_configs, livekit_call=livekit_call
        )
        == expected
    )
    assert run_pipeline.LIVEKIT_MAX_USER_IDLE_TIMEOUT_SECS == 8.0
    assert run_pipeline.DEFAULT_MAX_USER_IDLE_TIMEOUT_SECS == 10.0


def test_the_resolved_idle_timeout_reaches_the_user_aggregator():
    """``_run_pipeline_impl`` hands the resolved value to the user turn
    settings, where pipecat's idle controller reads it."""
    tree = _impl_tree()
    idle_names = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "resolve_max_user_idle_timeout"
    }
    passed = [
        ast.unparse(kw.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "user_idle_timeout"
    ]

    assert len(idle_names) == 1
    assert passed and set(passed) <= idle_names


def test_failure_exits_are_built_once_and_installed():
    calls = _impl_calls()

    [build] = calls["LiveKitFailureExits"]
    assert _kwarg(build, "is_realtime") == "is_realtime"
    assert len(calls["install_watchdog"]) == 1
    # Nothing builds a second state or watchdog beside the shared one.
    assert "CallFailureState" not in calls
    assert "resolve_safetynet_watchdog" not in calls
