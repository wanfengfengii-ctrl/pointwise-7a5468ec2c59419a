"""Deterministic recording adapter for the existing source/lease protocol."""
import asyncio
import copy


class PortalError(RuntimeError):
    pass


class Portal:
    def __init__(self, pages, gates=None, faults=None):
        self.pages = copy.deepcopy(pages)
        self.gates = dict(gates or {})
        self.faults = dict(faults or {})
        self.events = []
        self.live = set()
        self.calls = {}
        self.entered = {}

    async def _pause(self, stage, cursor):
        self.events.append([stage, cursor])
        self.entered.setdefault((stage, cursor), asyncio.Event()).set()
        gate = self.gates.get((stage, cursor))
        if gate is not None:
            await gate.wait()
        message = self.faults.get((stage, cursor))
        if message is not None:
            raise PortalError(message)

    async def wait(self, stage, cursor):
        await self.entered.setdefault((stage, cursor), asyncio.Event()).wait()

    async def open(self, cursor):
        self.calls[cursor] = self.calls.get(cursor, 0) + 1
        number = self.calls[cursor]
        try:
            await self._pause("open", cursor)
        except asyncio.CancelledError:
            self.events.append(["cancel-open", cursor])
            raise
        lease = Lease(self, cursor, number, copy.deepcopy(self.pages[cursor]))
        self.live.add((cursor, number))
        self.events.append(["leased", cursor, number])
        return lease


class Lease:
    def __init__(self, source, cursor, number, payload):
        self.source = source
        self.cursor = cursor
        self.number = number
        self.payload = payload

    async def ack(self):
        await self.source._pause("ack", self.cursor)

    async def release(self):
        # A release attempt consumes ownership even when its response is lost.
        self.source.live.remove((self.cursor, self.number))
        await self.source._pause("release", self.cursor)
