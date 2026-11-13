"""Synchronous scheduling core: ticket order, queue, and cleanup priority.

The only instance attribute is ``state``, a JSON-round-trippable dict of
dicts/lists/str/int/bool/None. It never holds tasks, leases, page rows,
exceptions, functions, or the source. step(event) maps one event to exactly
one effect; from_state(state) deep-copies a saved state back into an
equivalent controller.
"""
import copy


class Controller:
    def __init__(self, seeds, width):
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
        self.state = {
            "width": width,
            "serial": 0,
            "todo": copy.deepcopy(seeds),
            "pending": [],
            "scopes": {},
            "current": None,
            "released": [],
            "phase": "idle",
            "closed": False,
            "error_ref": None,
            "cleanup_ref": None,
            "dispose_queue": [],
        }

    @classmethod
    def from_state(cls, state):
        controller = cls.__new__(cls)
        controller.state = copy.deepcopy(state)
        return controller

    def step(self, event):
        state = self.state
        if state["closed"]:
            return ["stop", None, False]
        phase = state["phase"]
        if phase == "idle":
            if event[0] == "close":
                return self._begin_cleanup()
            return self._advance()
        if phase == "open":
            return self._advance()
        if phase == "wait":
            ticket = state["pending"][0]
            if event[0] == "ok":
                state["phase"] = "prepare"
                return ["prepare", ticket, state["scopes"][str(ticket)]]
            return self._fail("wait", ticket, event)
        if phase == "prepare":
            if event[0] == "batch":
                state["current"] = {"ticket": state["pending"][0],
                                    "n": event[1], "index": 0,
                                    "children": copy.deepcopy(event[2])}
                return self._advance()
            return self._fail("prepare", state["pending"][0], event)
        if phase == "ack":
            ticket = state["current"]["ticket"]
            if event[0] == "ok":
                state["todo"].extend(state["current"]["children"])
                # A release attempt consumes ownership, even one that fails
                # or is canceled; cleanup never retries it.
                state["released"].append(ticket)
                state["phase"] = "release"
                return ["release", ticket]
            return self._fail("ack", ticket, event)
        if phase == "release":
            ticket = state["current"]["ticket"]
            if event[0] == "ok":
                state["phase"] = "forget"
                return ["forget", ticket]
            return self._fail("release", ticket, event)
        if phase == "forget":
            ticket = state["current"]["ticket"]
            state["pending"].remove(ticket)
            del state["scopes"][str(ticket)]
            state["current"] = None
            return self._advance()
        if phase == "quiesce":
            acquired = event[1]
            released = state["released"]
            state["dispose_queue"] = [
                ticket for ticket in reversed(state["pending"])
                if ticket in acquired and ticket not in released]
            return self._cleanup_next()
        ticket = state["dispose_queue"].pop(0)
        if event[0] == "error" and state["cleanup_ref"] is None:
            state["cleanup_ref"] = ["dispose", ticket]
        return self._cleanup_next()

    def _advance(self):
        state = self.state
        if state["todo"] and len(state["pending"]) < state["width"]:
            cursor, scope = state["todo"].pop(0)
            ticket = state["serial"]
            state["serial"] = ticket + 1
            state["pending"].append(ticket)
            state["scopes"][str(ticket)] = scope
            state["phase"] = "open"
            return ["open", ticket, cursor, scope]
        if not state["pending"]:
            return self._finish()
        current = state["current"]
        if current is None:
            state["phase"] = "wait"
            return ["wait", state["pending"][0]]
        if current["index"] < current["n"]:
            index = current["index"]
            current["index"] = index + 1
            state["phase"] = "idle"
            return ["emit", current["ticket"], index]
        state["phase"] = "ack"
        return ["ack", current["ticket"]]

    def _fail(self, name, ticket, event):
        if event[0] == "abort":
            self.state["error_ref"] = ["cancel", None]
        else:
            self.state["error_ref"] = [name, ticket]
        return self._begin_cleanup()

    def _begin_cleanup(self):
        state = self.state
        if state["pending"]:
            state["phase"] = "quiesce"
            return ["quiesce", list(reversed(state["pending"]))]
        return self._finish()

    def _cleanup_next(self):
        state = self.state
        if state["dispose_queue"]:
            state["phase"] = "dispose"
            return ["dispose", state["dispose_queue"][0]]
        return self._finish()

    def _finish(self):
        state = self.state
        state["closed"] = True
        ref = state["error_ref"]
        if ref is None:
            ref = state["cleanup_ref"]
        return ["stop", ref, True]
