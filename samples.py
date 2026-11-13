def row(rid, rev, value, deleted=False):
    return {"id": rid, "rev": rev, "value": value, "deleted": deleted}


PAGES = {
    "root": {"scope": None,
             "rows": [row("b", 1, 9), row("a", 1, 5), row("b", 2, 12)],
             "next": [{"cursor": "leaf", "scope": None},
                      {"cursor": "leaf", "scope": "south"}]},
    "other": {"scope": "west", "rows": [row("c", 1, -3)], "next": []},
    "leaf": {"scope": None, "rows": [row("d", 3, 8, True)], "next": []}
}
SEEDS = [["root", "north"], ["other", "unused"]]
EXPECTED = [
    {"scope": "north", "id": "a", "rev": 1, "value": 5, "deleted": False},
    {"scope": "north", "id": "b", "rev": 2, "value": 12, "deleted": False},
    {"scope": "west", "id": "c", "rev": 1, "value": -3, "deleted": False},
    {"scope": "north", "id": "d", "rev": 3, "value": None, "deleted": True},
    {"scope": "south", "id": "d", "rev": 3, "value": None, "deleted": True}
]
TRACE = [
    ["start", 0, "root", "north"], ["start", 1, "other", "unused"],
    ["ready", 0], ["yield", 0, 0], ["yield", 0, 1],
    ["ack", 0], ["retire", 0], ["start", 2, "leaf", "north"],
    ["ready", 1], ["yield", 1, 0], ["ack", 1], ["retire", 1],
    ["start", 3, "leaf", "south"], ["ready", 2], ["yield", 2, 0],
    ["ack", 2], ["retire", 2], ["ready", 3], ["yield", 3, 0],
    ["ack", 3], ["retire", 3], ["closed"]
]
