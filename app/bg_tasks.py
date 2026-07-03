"""Tracked fire-and-forget asyncio tasks.

FastAPI BackgroundTasks (.add_task) run inside the request lifecycle and are
already awaited by uvicorn's graceful shutdown. Detached asyncio.create_task(...)
work is NOT — a deploy/SIGTERM cancels it mid-flight. spawn() registers such
tasks so drain() can wait for them during lifespan shutdown before the loop stops.
"""
import asyncio

_pending: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """asyncio.create_task, but tracked so drain() awaits it on shutdown."""
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def drain(timeout: float = 25.0) -> None:
    """Wait up to `timeout`s for tracked tasks to finish. Called on shutdown."""
    if not _pending:
        return
    _, still_pending = await asyncio.wait(set(_pending), timeout=timeout)
    if still_pending:
        print(f"WARN: {len(still_pending)} background task(s) did not finish "
              f"within {timeout}s of shutdown")


if __name__ == "__main__":
    async def _demo():
        spawn(asyncio.sleep(0.05))
        spawn((lambda: asyncio.sleep(0.05))())  # second slow task
        assert len(_pending) == 2
        await drain(timeout=1.0)
        assert not _pending, "drain must clear finished tasks"
        # a task slower than the drain timeout is left pending, not awaited forever
        slow = spawn(asyncio.sleep(5))
        await drain(timeout=0.1)
        assert slow in _pending
        slow.cancel()
        print("ok")
    asyncio.run(_demo())
