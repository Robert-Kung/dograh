"""Fake answering participant for the engine-free exit tests (answer-before-refer).

Replaces ``livekit_answer.answering`` so the safetynet / overflow tests see the
exit sequence — prompts played, REFER, room delete — in one ordered log
without rtc. ``fail_at`` makes one step raise ``AnswerFailed(stage)``:
``enter`` (before yielding, e.g. ``connect``), ``transfer`` / ``end`` (that
prompt's play).
"""

import asyncio
from contextlib import asynccontextmanager

from api.services.pipecat import livekit_answer
from api.services.pipecat.livekit_answer import AnswerFailed


class FakeAnswering:
    def __init__(self, log: list):
        self.log = log
        self.entered: list[str] = []
        self.fail_at: dict[str, str] = {}  # step -> stage
        self.raise_at: dict[str, BaseException] = {}  # step -> exception
        self.hang_at: set[str] = set()

    async def _step(self, step: str) -> None:
        if step in self.hang_at:
            await asyncio.Event().wait()
        if step in self.raise_at:
            raise self.raise_at[step]
        if step in self.fail_at:
            raise AnswerFailed(self.fail_at[step])

    def __call__(self, room_name, *, reason, workflow_run_id=None):
        fake = self

        class _Ans:
            async def play(self, prompt):
                await fake._step(prompt)
                fake.log.append(("play", prompt))

        @asynccontextmanager
        async def _cm():
            await fake._step("enter")
            fake.entered.append(room_name)
            fake.log.append(("answer", room_name))
            try:
                yield _Ans()
            finally:
                fake.log.append(("disconnect", room_name))

        return _cm()


def install(monkeypatch, log: list) -> FakeAnswering:
    fake = FakeAnswering(log)
    monkeypatch.setattr(livekit_answer, "answering", fake)
    return fake
