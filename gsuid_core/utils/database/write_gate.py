"""进程内 SQLite 单写者闸门。

``@with_session`` 与 ``db_write_guard`` 共用这一把。
优先级看被装饰函数的定义模块（不是调用方），同一任务可重入。
排队超过 ``GATE_WAIT_S`` 就失败。占锁超时会取消该任务，等它退出后再交接。
"""

import time
import asyncio
import threading
from types import TracebackType
from collections import deque
from collections.abc import Callable

# 一条正常写的预算是 10 秒。再留一截给收尾，超时就认为闸门被占死。
GATE_WAIT_S = 20.0


class WriteGateTimeout(Exception):
    """排队等写闸门超过 ``GATE_WAIT_S``。"""


class _Waiter:
    __slots__ = ("cancel", "fut", "tid", "loop")

    def __init__(self, tid: int, fut: asyncio.Future[None], cancel: Callable[[], None]) -> None:
        self.tid = tid
        self.fut = fut
        self.cancel = cancel
        self.loop = fut.get_loop()

    def wake(self) -> None:
        if not self.fut.done():
            self.fut.set_result(None)


class _Hold:
    __slots__ = ("_core", "_gate")

    def __init__(self, gate: "SqliteWriteGate", core: bool) -> None:
        self._gate = gate
        self._core = core

    async def __aenter__(self) -> None:
        await self._gate.enter(self._core)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._gate.leave()


class SqliteWriteGate:
    """单写者队列。``async with`` 走框架优先级。"""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._owner: int | None = None
        self._acquired_at: float | None = None
        self._cancel_owner: Callable[[], None] | None = None
        self._evicting = False
        self._depth = 0
        self._core: deque[_Waiter] = deque()
        self._plugin: deque[_Waiter] = deque()

    def hold(self, *, core: bool) -> _Hold:
        return _Hold(self, core)

    async def __aenter__(self) -> "SqliteWriteGate":
        await self.enter(True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.leave()

    def _identity(self) -> tuple[int, Callable[[], None]]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("sqlite write gate must run inside a task")
        loop = asyncio.get_running_loop()

        def _cancel_on_owner_loop() -> None:
            if not task.done():
                task.cancel()

        def _cancel() -> None:
            loop.call_soon_threadsafe(_cancel_on_owner_loop)

        return id(task), _cancel

    async def enter(self, core: bool) -> None:
        tid, cancel = self._identity()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        waiter = _Waiter(tid, fut, cancel)
        with self._state_lock:
            if self._owner == tid:
                self._depth += 1
                return
            if self._owner is None and not self._core and not self._plugin:
                self._take(tid, cancel)
                return
            queue = self._core if core else self._plugin
            queue.append(waiter)
        try:
            done, _pending = await asyncio.wait({fut}, timeout=GATE_WAIT_S)
        except asyncio.CancelledError:
            with self._state_lock:
                if self._owner == tid:
                    self._handoff()
                else:
                    self._discard(fut)
            raise
        with self._state_lock:
            if fut not in done:
                self._discard(fut)
                # 交接和超时撞在一起时，自己可能已经是 owner，必须先交出去。
                if self._owner == tid:
                    self._handoff()
                else:
                    self._steal_if_overdue()
                raise WriteGateTimeout()
            if self._owner != tid:
                raise RuntimeError("sqlite write gate woke the wrong task")

    def leave(self) -> None:
        tid, _cancel = self._identity()
        with self._state_lock:
            if self._owner != tid:
                return
            self._depth -= 1
            if self._depth > 0:
                return
            self._handoff()

    def _discard(self, fut: asyncio.Future[None]) -> None:
        self._core = deque(item for item in self._core if item.fut is not fut)
        self._plugin = deque(item for item in self._plugin if item.fut is not fut)

    def _pop(self) -> _Waiter | None:
        for queue in (self._core, self._plugin):
            while queue:
                waiter = queue.popleft()
                if waiter.fut.done():
                    continue
                return waiter
        return None

    def _take(self, tid: int, cancel: Callable[[], None]) -> None:
        self._owner = tid
        self._cancel_owner = cancel
        self._depth = 1
        self._acquired_at = time.monotonic()
        self._evicting = False

    def _steal_if_overdue(self) -> None:
        acquired = self._acquired_at
        if self._evicting or self._owner is None or acquired is None:
            return
        # 留 50ms，避免计时刚好卡在期限上把正常写取消。
        if time.monotonic() - acquired + 0.05 < GATE_WAIT_S:
            return
        from gsuid_core.i18n import t
        from gsuid_core.logger import logger

        budget = str(int(GATE_WAIT_S)) if GATE_WAIT_S.is_integer() else str(GATE_WAIT_S)
        logger.error(t("log.database.write_gate_stolen", budget=budget))
        # 先取消，等占锁任务自己 leave 再交接，避免两个人同时写。
        self._evicting = True
        cancel = self._cancel_owner
        if cancel is not None:
            cancel()

    def _handoff(self) -> None:
        self._owner = None
        self._cancel_owner = None
        self._depth = 0
        self._acquired_at = None
        self._evicting = False
        waiter = self._pop()
        if waiter is None:
            return
        self._take(waiter.tid, waiter.cancel)
        # Future 必须由所属事件循环唤醒，插件可能在另一线程调用数据库。
        waiter.loop.call_soon_threadsafe(waiter.wake)


sqlite_write_gate = SqliteWriteGate()
