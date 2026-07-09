"""Fire-and-forget background tasks with strong references.

The event loop only holds a weak reference to a task; without a strong ref a
GC'd task silently drops the work — unacceptable for safetynet fallbacks and
alert delivery (S-L3-SAFETYNET / S-L7-OBS).
"""

import asyncio

_background_tasks: set = set()


def spawn(coro) -> asyncio.Task:
    """Run ``coro`` in the background, holding a strong reference until done."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
