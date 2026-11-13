"""Pure page-interpretation rules: validation, revision resolution, normalization.

transform() performs no I/O, holds no state, and neither mutates nor retains
the caller's payload; every returned structure is freshly built.
"""


class RecordError(ValueError):
    pass


def transform(payload, inherited):
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
