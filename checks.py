import asyncio
import copy
import json
import sys
from portal import Portal, PortalError
from samples import PAGES, SEEDS, EXPECTED, TRACE, row
from walker import Walker, RecordError


async def anext(iterator):
    return await iterator.__anext__()


async def collect(walker):
    return [item async for item in walker]


async def raises(kind, message, awaitable):
    try:
        await awaitable
    except kind as exc:
        assert str(exc) == message, (type(exc).__name__, str(exc), message)
        return exc
    raise AssertionError("exception not raised: " + message)


async def normal():
    pages, seeds = copy.deepcopy(PAGES), copy.deepcopy(SEEDS)
    portal = Portal(pages)
    walker = Walker(portal, seeds)
    assert await collect(walker) == EXPECTED
    assert walker.trace == TRACE, walker.trace
    assert not portal.live
    assert portal.calls == {"root": 1, "other": 1, "leaf": 2}
    assert pages == PAGES and seeds == SEEDS
    await walker.aclose()
    assert walker.trace == TRACE
    empty = Walker(Portal({}), [], 3)
    assert await collect(empty) == []
    assert empty.trace == [["closed"]]


async def partial_and_cleanup():
    portal = Portal(PAGES)
    walker = Walker(portal, SEEDS)
    assert await anext(walker) == EXPECTED[0]
    await portal.wait("open", "other")
    # Force completion through its task, without relying on an elapsed delay.
    # The following API call yields the second row but does not acknowledge root.
    assert await anext(walker) == EXPECTED[1]
    await walker.aclose()
    assert ["ack", "root"] not in portal.events
    assert [x for x in portal.events if x[0] == "release"] == [
        ["release", "other"], ["release", "root"]]
    assert walker.trace[-3:] == [["dispose", 1], ["dispose", 0], ["closed"]]
    assert not portal.live
    await walker.aclose()
    await raises(StopAsyncIteration, "", anext(walker))


async def ordered_errors():
    gate = asyncio.Event()
    portal = Portal(PAGES, {("open", "root"): gate},
                    {("open", "other"): "LATER"})
    walker = Walker(portal, SEEDS)
    task = asyncio.create_task(anext(walker))
    await portal.wait("open", "other")
    assert not task.done()
    gate.set()
    assert await task == EXPECTED[0]
    assert await anext(walker) == EXPECTED[1]
    await raises(PortalError, "LATER", anext(walker))
    assert ["ack", "root"] in portal.events
    assert not portal.live
    # A malformed earlier page outranks an already failed later fetch and cleanup.
    pages = copy.deepcopy(PAGES)
    pages["root"]["rows"].append(row("x", True, 1))
    portal = Portal(pages, faults={("open", "other"): "LATER",
                                  ("release", "root"): "DISPOSE"})
    walker = Walker(portal, SEEDS)
    await raises(RecordError, "REV", anext(walker))
    assert walker.trace[-1] == ["closed"] and not portal.live


async def cancellation_and_busy():
    gate = asyncio.Event()
    portal = Portal(PAGES, {("open", "root"): gate})
    walker = Walker(portal, SEEDS)
    task = asyncio.create_task(anext(walker))
    await portal.wait("open", "root")
    await portal.wait("open", "other")
    await raises(RuntimeError, "BUSY", anext(walker))
    await raises(RuntimeError, "BUSY", walker.aclose())
    task.cancel()
    await raises(asyncio.CancelledError, "", task)
    assert ["cancel-open", "root"] in portal.events
    assert not portal.live and walker.trace[-1] == ["closed"]
    # Cancellation while ack is blocked must not publish discovered child cursors.
    gate = asyncio.Event()
    portal = Portal(PAGES, {("ack", "root"): gate})
    walker = Walker(portal, [["root", "north"]])
    await anext(walker)
    await anext(walker)
    task = asyncio.create_task(anext(walker))
    await portal.wait("ack", "root")
    task.cancel()
    await raises(asyncio.CancelledError, "", task)
    assert portal.calls == {"root": 1}
    assert not portal.live


async def cleanup_priority():
    portal = Portal(PAGES, faults={("release", "other"): "HIGH",
                                  ("release", "root"): "LOW"})
    walker = Walker(portal, SEEDS)
    await anext(walker)
    await portal.wait("open", "other")
    await raises(PortalError, "HIGH", walker.aclose())
    assert not portal.live
    assert walker.trace[-3:] == [["dispose", 1], ["dispose", 0], ["closed"]]
    portal = Portal(PAGES, faults={("ack", "root"): "ACK",
                                  ("release", "root"): "RELEASE"})
    walker = Walker(portal, [["root", "north"]])
    await anext(walker)
    await anext(walker)
    await raises(PortalError, "ACK", anext(walker))
    assert portal.calls == {"root": 1} and not portal.live
    assert [x for x in portal.events if x[0] == "release"] == [["release", "root"]]


async def empty_page_and_revision():
    pages = {
        "blank": {"scope": None, "rows": [],
                  "next": [{"cursor": "tail", "scope": "z"}]},
        "tail": {"scope": None,
                 "rows": [row("a", 2, 5), row("a", 1, 9), row("a", 2, 5)],
                 "next": []}
    }
    portal = Portal(pages)
    walker = Walker(portal, [["blank", "x"]], 1)
    assert await anext(walker) == {"scope": "z", **row("a", 2, 5)}
    assert ["ack", "blank"] in portal.events
    await walker.aclose()
    pages["tail"]["rows"].append(row("a", 2, 6))
    portal = Portal(pages)
    walker = Walker(portal, [["tail", "x"]])
    await raises(RecordError, "CONFLICT:a", anext(walker))
    assert not portal.live


async def verify():
    for check in (normal, partial_and_cleanup, ordered_errors,
                  cancellation_and_busy, cleanup_priority, empty_page_and_revision):
        await check()
    print("BEHAVIOR_REGRESSION_OK")


async def demo():
    portal = Portal(PAGES)
    walker = Walker(portal, SEEDS)
    rows = await collect(walker)
    print(json.dumps({"rows": rows, "trace": walker.trace,
                      "source_events": portal.events,
                      "unreleased": sorted(portal.live)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if mode == "verify":
        asyncio.run(verify())
    elif mode == "demo":
        asyncio.run(demo())
    else:
        raise SystemExit("usage: python3 checks.py verify|demo")
