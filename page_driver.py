"""Asynchronous effect driver: owns tasks, leases, row payloads, and errors.

The Executor only executes controller effects, stores opaque live objects,
and translates replies. Ticket order, queue growth, ack timing, row position,
and error priority all belong to the controller. Original exception objects
are kept under [effect, ticket] references so stop re-raises them unwrapped.
"""
import asyncio
import record_rules


class Executor:
    def __init__(self, source, controller):
        self.source = source
        self.controller = controller
        self.tasks = {}
        self.leases = {}
        self.rows = {}
        self.errors = {}
        self.trace = []
        self.busy = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.busy:
            raise RuntimeError("BUSY")
        self.busy = True
        try:
            outcome = await self._run(["next"])
            if outcome[0] == "emit":
                return outcome[1]
            error = self._settle(outcome[1], outcome[2])
            if error is not None:
                raise error
            raise StopAsyncIteration
        finally:
            self.busy = False

    async def aclose(self):
        if self.busy:
            raise RuntimeError("BUSY")
        self.busy = True
        try:
            outcome = await self._run(["close"])
            error = self._settle(outcome[1], outcome[2])
            if error is not None:
                raise error
        finally:
            self.busy = False

    async def _run(self, event):
        while True:
            effect = self.controller.step(event)
            kind = effect[0]
            if kind == "emit":
                self.trace.append(["yield", effect[1], effect[2]])
                return ("emit", self.rows[effect[1]][effect[2]])
            if kind == "stop":
                return ("stop", effect[1], effect[2])
            event = await self._perform(effect)

    def _settle(self, ref, first):
        if first:
            self.trace.append(["closed"])
        error = None
        if ref is not None:
            error = self.errors[(ref[0], ref[1])]
        self.tasks.clear()
        self.leases.clear()
        self.rows.clear()
        self.errors.clear()
        return error

    def _abort(self, exc):
        self.errors[("cancel", None)] = exc
        return ["abort"]

    def _fail(self, name, ticket, exc):
        self.errors[(name, ticket)] = exc
        self.trace.append(["error", type(exc).__name__, str(exc)])
        return ["error"]

    async def _perform(self, effect):
        kind = effect[0]
        if kind == "open":
            _, ticket, cursor, scope = effect
            self.tasks[ticket] = asyncio.create_task(self.source.open(cursor))
            self.trace.append(["start", ticket, cursor, scope])
            return ["ok"]
        if kind == "wait":
            ticket = effect[1]
            try:
                self.leases[ticket] = await asyncio.shield(self.tasks[ticket])
            except asyncio.CancelledError as exc:
                return self._abort(exc)
            except BaseException as exc:
                return self._fail("wait", ticket, exc)
            return ["ok"]
        if kind == "prepare":
            ticket, scope = effect[1], effect[2]
            try:
                result = record_rules.transform(self.leases[ticket].payload, scope)
            except BaseException as exc:
                return self._fail("prepare", ticket, exc)
            self.rows[ticket] = result["rows"]
            self.trace.append(["ready", ticket])
            return ["batch", len(result["rows"]), result["children"]]
        if kind == "ack":
            ticket = effect[1]
            try:
                await self.leases[ticket].ack()
            except asyncio.CancelledError as exc:
                return self._abort(exc)
            except BaseException as exc:
                return self._fail("ack", ticket, exc)
            self.trace.append(["ack", ticket])
            return ["ok"]
        if kind == "release":
            ticket = effect[1]
            try:
                await self.leases[ticket].release()
            except asyncio.CancelledError as exc:
                return self._abort(exc)
            except BaseException as exc:
                return self._fail("release", ticket, exc)
            self.trace.append(["retire", ticket])
            return ["ok"]
        if kind == "forget":
            ticket = effect[1]
            del self.tasks[ticket]
            del self.leases[ticket]
            del self.rows[ticket]
            return ["ok"]
        if kind == "quiesce":
            tickets = effect[1]
            for ticket in tickets:
                task = self.tasks[ticket]
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(
                *[self.tasks[ticket] for ticket in tickets],
                return_exceptions=True)
            for ticket, result in zip(tickets, results):
                if not isinstance(result, BaseException):
                    self.leases[ticket] = result
            return ["leases", [t for t in tickets if t in self.leases]]
        ticket = effect[1]
        try:
            await self.leases[ticket].release()
        except BaseException as exc:
            self.errors[("dispose", ticket)] = exc
            self.trace.append(["dispose", ticket])
            return ["error"]
        self.trace.append(["dispose", ticket])
        return ["ok"]
