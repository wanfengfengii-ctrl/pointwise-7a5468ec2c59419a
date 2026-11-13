"""Legacy ordered page collector; public behavior is specified in CONTRACT.md."""
import asyncio
import copy
from collections import deque


class RecordError(ValueError):
    pass


class Walker:
    def __init__(self, source, seeds, width=2):
        if type(width) is not int or width < 1:
            raise ValueError("WIDTH")
        if not isinstance(seeds, list):
            raise ValueError("SEEDS")
        for seed in seeds:
            if not isinstance(seed, list) or len(seed) != 2:
                raise ValueError("SEED")
            if not isinstance(seed[0], str) or not seed[0]:
                raise ValueError("CURSOR")
            if not isinstance(seed[1], str) or not seed[1]:
                raise ValueError("SCOPE")
        self.source = source
        self.width = width
        self.todo = deque(copy.deepcopy(seeds))
        self.pending = {}
        self.serial = 0
        self.active = None
        self.disposed = set()
        self.closed = False
        self.busy = False
        self.trace = []

    def __aiter__(self):
        return self

    def _fill(self):
        while self.todo and len(self.pending) < self.width:
            cursor, scope = self.todo.popleft()
            ticket = self.serial
            self.serial += 1
            task = asyncio.create_task(self.source.open(cursor))
            self.pending[ticket] = (task, scope)
            self.trace.append(["start", ticket, cursor, scope])

    def _prepare(self, payload, inherited):
        # Validation order, revision resolution, and scheduling context are tangled here.
        if not isinstance(payload, dict) or set(payload) != {"scope", "rows", "next"}:
            raise RecordError("PAGE")
        scope = payload["scope"]
        if scope is None:
            scope = inherited
        if not isinstance(scope, str) or not scope:
            raise RecordError("SCOPE")
        if not isinstance(payload["rows"], list):
            raise RecordError("ROWS")
        best = {}
        for row in payload["rows"]:
            if not isinstance(row, dict) or set(row) != {"id", "rev", "value", "deleted"}:
                raise RecordError("ROW")
            if not isinstance(row["id"], str) or not row["id"]:
                raise RecordError("ID")
            if type(row["rev"]) is not int or row["rev"] < 1:
                raise RecordError("REV")
            if type(row["value"]) is not int:
                raise RecordError("VALUE")
            if type(row["deleted"]) is not bool:
                raise RecordError("DELETED")
            old = best.get(row["id"])
            if old is not None and old["rev"] == row["rev"] and old != row:
                raise RecordError("CONFLICT:" + row["id"])
            if old is None or old["rev"] < row["rev"]:
                best[row["id"]] = row
        if not isinstance(payload["next"], list):
            raise RecordError("NEXT")
        children = []
        for link in payload["next"]:
            if not isinstance(link, dict) or set(link) != {"cursor", "scope"}:
                raise RecordError("LINK")
            if not isinstance(link["cursor"], str) or not link["cursor"]:
                raise RecordError("CURSOR")
            child_scope = scope if link["scope"] is None else link["scope"]
            if not isinstance(child_scope, str) or not child_scope:
                raise RecordError("SCOPE")
            children.append([link["cursor"], child_scope])
        rows = []
        for rid in sorted(best):
            row = best[rid]
            rows.append({"scope": scope, "id": rid, "rev": row["rev"],
                         "value": None if row["deleted"] else row["value"],
                         "deleted": row["deleted"]})
        return {"rows": rows, "children": children}

    async def _shutdown(self):
        if self.closed:
            return None
        self.closed = True
        ordered = sorted(self.pending, reverse=True)
        for ticket in ordered:
            task = self.pending[ticket][0]
            if not task.done():
                task.cancel()
        results = await asyncio.gather(
            *(self.pending[t][0] for t in ordered), return_exceptions=True)
        error = None
        for ticket, result in zip(ordered, results):
            if isinstance(result, BaseException) or ticket in self.disposed:
                continue
            self.disposed.add(ticket)
            try:
                await result.release()
            except BaseException as exc:
                if error is None:
                    error = exc
            self.trace.append(["dispose", ticket])
        self.pending.clear()
        self.todo.clear()
        self.active = None
        self.trace.append(["closed"])
        return error

    async def __anext__(self):
        if self.busy:
            raise RuntimeError("BUSY")
        if self.closed:
            raise StopAsyncIteration
        self.busy = True
        try:
            while True:
                self._fill()
                if not self.pending:
                    await self._shutdown()
                    raise StopAsyncIteration
                ticket = min(self.pending)
                task, inherited = self.pending[ticket]
                if self.active is None:
                    lease = await asyncio.shield(task)
                    batch = self._prepare(lease.payload, inherited)
                    self.active = [ticket, lease, batch, 0]
                    self.trace.append(["ready", ticket])
                _, lease, batch, index = self.active
                if index < len(batch["rows"]):
                    self.active[3] += 1
                    self.trace.append(["yield", ticket, index])
                    return batch["rows"][index]
                await lease.ack()
                self.trace.append(["ack", ticket])
                self.todo.extend(batch["children"])
                # Attempted release, including one that raises, is never retried.
                self.disposed.add(ticket)
                await lease.release()
                self.trace.append(["retire", ticket])
                del self.pending[ticket]
                self.active = None
        except StopAsyncIteration:
            raise
        except BaseException as primary:
            self.trace.append(["error", type(primary).__name__, str(primary)])
            await self._shutdown()
            raise
        finally:
            self.busy = False

    async def aclose(self):
        if self.busy:
            raise RuntimeError("BUSY")
        self.busy = True
        try:
            error = await self._shutdown()
            if error is not None:
                raise error
        finally:
            self.busy = False
