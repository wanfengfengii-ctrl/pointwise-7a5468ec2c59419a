"""Compatibility facade: forwards the legacy Walker API to the split parts."""
from control import Controller
from page_driver import Executor
from record_rules import RecordError


class Walker:
    def __init__(self, source, seeds, width=2):
        self._executor = Executor(source, Controller(seeds, width))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._executor.__anext__()

    async def aclose(self):
        await self._executor.aclose()

    @property
    def trace(self):
        return self._executor.trace
