"""Frozen architecture checks. The legacy baseline intentionally lacks these boundaries."""
import ast
import asyncio
import copy
import importlib
import json
from pathlib import Path
from portal import Portal
from samples import PAGES, row

ROOT = Path(__file__).resolve().parent


def static_checks():
    allowed = {
        "control": {"copy"},
        "record_rules": {"copy"},
        "page_driver": {"asyncio", "record_rules"},
        "walker": {"control", "record_rules", "page_driver"},
    }
    for name, imports in allowed.items():
        tree = ast.parse((ROOT / (name + ".py")).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name in imports for a in node.names), (name, "import")
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module in imports, (name, "from")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__", "open"}
        if name == "walker":
            forbidden = (ast.If, ast.IfExp, ast.For, ast.While, ast.Try,
                         ast.With, ast.AsyncWith, ast.ListComp, ast.DictComp,
                         ast.SetComp, ast.GeneratorExp)
            assert not any(isinstance(n, forbidden) for n in ast.walk(tree))
            classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
            assert len(classes) == 1 and classes[0].name == "Walker"
            methods = [n.name for n in classes[0].body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            assert set(methods) <= {"__init__", "__aiter__", "__anext__", "aclose", "trace"}
            assert not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                           for n in tree.body)


def plain(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is list:
        for item in value:
            plain(item)
        return
    assert type(value) is dict and all(type(k) is str for k in value), type(value)
    for item in value.values():
        plain(item)


def policy_checks():
    rules = importlib.import_module("record_rules")
    from walker import RecordError
    assert rules.RecordError is RecordError
    payload = copy.deepcopy(PAGES["root"])
    before = copy.deepcopy(payload)
    result = rules.transform(payload, "inherited")
    assert payload == before
    assert result == {
        "rows": [{"scope": "inherited", **row("a", 1, 5)},
                 {"scope": "inherited", **row("b", 2, 12)}],
        "children": [["leaf", "inherited"], ["leaf", "south"]]
    }
    result["rows"][0]["value"] = 999
    assert rules.transform(payload, "inherited")["rows"][0]["value"] == 5


def control_checks():
    Controller = importlib.import_module("control").Controller

    def start(seeds, width):
        before = copy.deepcopy(seeds)
        controller = Controller(seeds, width)
        assert seeds == before
        return controller

    def step(controller, event):
        # Replace the live controller after EVERY effect with JSON-restored state.
        assert set(vars(controller)) == {"state"}
        plain(controller.state)
        saved = json.loads(json.dumps(controller.state))
        other_input = copy.deepcopy(saved)
        other = Controller.from_state(other_input)
        assert other_input == saved
        original_event = copy.deepcopy(event)
        a = controller.step(event)
        b = other.step(copy.deepcopy(event))
        assert event == original_event and a == b, (event, a, b)
        assert other_input == saved, "from_state retained caller state"
        plain(a)
        assert controller.state == other.state
        plain(other.state)
        restored = Controller.from_state(json.loads(json.dumps(other.state)))
        return restored, a

    def expect(controller, event, action):
        controller, got = step(controller, event)
        assert got == action, (event, got, action)
        return controller

    c = start([["a", "x"], ["b", "y"]], 2)
    script = [
        (["next"], ["open", 0, "a", "x"]),
        (["ok"], ["open", 1, "b", "y"]),
        (["ok"], ["wait", 0]),
        (["ok"], ["prepare", 0, "x"]),
        (["batch", 2, [["c", "z"]]], ["emit", 0, 0]),
        (["next"], ["emit", 0, 1]),
        (["next"], ["ack", 0]),
        (["ok"], ["release", 0]),
        (["ok"], ["forget", 0]),
        (["ok"], ["open", 2, "c", "z"]),
        (["ok"], ["wait", 1]),
        (["error"], ["quiesce", [2, 1]]),
        (["leases", [2]], ["dispose", 2]),
        (["error"], ["stop", ["wait", 1], True]),
        (["close"], ["stop", None, False]),
        (["next"], ["stop", None, False]),
    ]
    for event, action in script:
        c = expect(c, event, action)

    # No original failure: retain first descending cleanup failure, but clean all.
    c = start([["a", "x"], ["b", "y"]], 2)
    for event, action in [
        (["next"], ["open", 0, "a", "x"]),
        (["ok"], ["open", 1, "b", "y"]),
        (["ok"], ["wait", 0]),
        (["ok"], ["prepare", 0, "x"]),
        (["batch", 1, []], ["emit", 0, 0]),
        (["close"], ["quiesce", [1, 0]]),
        (["leases", [1, 0]], ["dispose", 1]),
        (["error"], ["dispose", 0]),
        (["error"], ["stop", ["dispose", 1], True]),
    ]:
        c = expect(c, event, action)

    # A canceled release was attempted; do not release that same lease twice.
    c = start([["a", "x"]], 1)
    for event, action in [
        (["next"], ["open", 0, "a", "x"]),
        (["ok"], ["wait", 0]),
        (["ok"], ["prepare", 0, "x"]),
        (["batch", 0, [["later", "x"]]], ["ack", 0]),
        (["ok"], ["release", 0]),
        (["abort"], ["quiesce", [0]]),
        (["leases", [0]], ["stop", ["cancel", None], True]),
    ]:
        c = expect(c, event, action)
    c = start([["never", "x"]], 1)
    expect(c, ["close"], ["stop", None, True])
    c = start([], 1)
    expect(c, ["next"], ["stop", None, True])


async def driver_checks():
    driver_module = importlib.import_module("page_driver")
    rules = importlib.import_module("record_rules")
    sentinel = object()

    class Payload:
        def __getattribute__(self, name):
            raise AssertionError("raw payload inspected outside transform")

    class Lease:
        payload = Payload()
        async def ack(self):
            raise AssertionError("script did not request ack")
        async def release(self):
            pass

    class Source:
        async def open(self, cursor):
            assert cursor == "opaque"
            return Lease()

    class Tape:
        def __init__(self):
            self.position = 0
        def step(self, event):
            script = [
                (["next"], ["open", 7, "opaque", "s"]),
                (["ok"], ["wait", 7]),
                (["ok"], ["prepare", 7, "s"]),
                (["batch", 2, []], ["emit", 7, 1]),
                (["close"], ["quiesce", [7]]),
                (["leases", [7]], ["dispose", 7]),
                (["ok"], ["stop", None, True]),
            ]
            expected, result = script[self.position]
            assert event == expected, (event, expected)
            self.position += 1
            return copy.deepcopy(result)

    def substitute(payload, scope):
        assert type(payload) is Payload and scope == "s"
        return {"rows": [object(), sentinel], "children": []}

    old = rules.transform
    rules.transform = substitute
    try:
        Executor = importlib.reload(driver_module).Executor
        tape = Tape()
        executor = Executor(Source(), tape)
        assert await executor.__anext__() is sentinel
        assert set(vars(executor)) <= {
            "source", "controller", "tasks", "leases", "rows", "errors", "trace", "busy"}
        await executor.aclose()
        assert tape.position == 7
        assert executor.trace == [
            ["start", 7, "opaque", "s"], ["ready", 7], ["yield", 7, 1],
            ["dispose", 7], ["closed"]]
    finally:
        rules.transform = old
        Executor = importlib.reload(driver_module).Executor

    # Original exception identity must survive control references and cleanup.
    from control import Controller
    primary = RuntimeError("identity")
    secondary = RuntimeError("cleanup")
    class BadLease:
        payload = PAGES["root"]
        async def ack(self):
            raise primary
        async def release(self):
            raise secondary
    class BadSource:
        async def open(self, cursor):
            return BadLease()
    executor = Executor(BadSource(), Controller([["root", "north"]], 1))
    await executor.__anext__()
    await executor.__anext__()
    try:
        await executor.__anext__()
    except RuntimeError as exc:
        assert exc is primary
    else:
        raise AssertionError("missing original exception")


def verify():
    static_checks()
    policy_checks()
    control_checks()
    asyncio.run(driver_checks())
    print("BOUNDARIES_OK")


if __name__ == "__main__":
    verify()
