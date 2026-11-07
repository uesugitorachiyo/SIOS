"""Bounded daemon workers for read-only successor calls."""
import asyncio
import threading
from .successor_guard import inspect


class SuccessorBridge:
    def __init__(self, call, *, max_inflight=1, timeout=20):
        self.call = call
        self.gate = asyncio.Semaphore(max_inflight)
        self.timeout = timeout

    async def _call(self, operation, params):
        if operation not in {"read", "memory_index_search"}:
            raise ValueError("unsupported read-only operation")
        await self.gate.acquire()
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def complete(value, error):
            try:
                if not future.done():
                    if error is None:
                        future.set_result(value)
                    else:
                        future.set_exception(error)
                if not future.cancelled():
                    future.exception()  # consume errors even after caller timeout
            finally:
                self.gate.release()

        def worker():
            try:
                value = self.call(operation, params)
                error = None
            except BaseException as exc:
                value, error = None, exc
            try:
                loop.call_soon_threadsafe(complete, value, error)
            except RuntimeError:
                # Shutdown closed the loop; the daemon thread exits on process
                # termination. It must never perform side-effecting operations.
                pass

        try:
            thread = threading.Thread(target=worker, name='sios-successor-read', daemon=True)
            thread.start()
        except BaseException:
            self.gate.release()
            raise
        return await asyncio.shield(future)

    async def inspect(self, predecessor_path):
        async def read(path):
            return await self._call('read', {'relative': path})
        async def search(query):
            return await self._call('memory_index_search', {'query': query})
        try:
            async with asyncio.timeout(self.timeout):
                return await inspect(predecessor_path, read=read, search=search)
        except TimeoutError:
            return {'status':'unknown','reason':'deadline','current_state':'unknown','successors':[]}
