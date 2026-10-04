import sqlite3


class Relations:
    def __init__(self, db):
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_dependencies("
            "card_id TEXT NOT NULL,"
            " basis_id TEXT NOT NULL,"
            " basis_version INTEGER NOT NULL,"
            " PRIMARY KEY(card_id,basis_id))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_dependencies_basis "
            "ON knowledge_dependencies(basis_id)"
        )

    def list(self, card_id):
        rows = self.db.execute(
            "SELECT basis_id, basis_version FROM knowledge_dependencies "
            "WHERE card_id = ? ORDER BY basis_id",
            (card_id,),
        ).fetchall()
        return [
            {"card_id": r["basis_id"], "version": r["basis_version"]} for r in rows
        ]

    @staticmethod
    def _is_card_id(v):
        return (
            isinstance(v, str)
            and len(v) == 32
            and all(c in "0123456789abcdef" for c in v)
        )

    @staticmethod
    def _is_version(v):
        return isinstance(v, int) and not isinstance(v, bool) and v > 0

    def _card_row(self, card_id):
        return self.db.execute(
            "SELECT id, project, state, version FROM knowledge_cards WHERE id = ?",
            (card_id,),
        ).fetchone()

    def _deps(self, card_id):
        return self.db.execute(
            "SELECT basis_id AS card_id, basis_version AS version FROM knowledge_dependencies "
            "WHERE card_id = ?",
            (card_id,),
        ).fetchall()

    def _check_ancestry(self, owner_id, refs, changing_ids, seen, depth):
        if depth > 32:
            return False
        for ref in refs:
            bid = ref["card_id"]
            if bid == owner_id:
                return False
            if bid in changing_ids:
                return False
            row = self._card_row(bid)
            if row is None:
                return False
            if row["state"] != "active":
                return False
            if row["version"] != ref["version"]:
                return False
            if bid in seen:
                return False
            if not self._check_ancestry(
                owner_id, self._deps(bid), changing_ids, seen | {bid}, depth + 1
            ):
                return False
        return True

    def validate(self, project, owner_id, refs, changing_ids=()):
        if refs is None:
            refs = []
        if not isinstance(refs, list) or len(refs) > 12:
            raise ValueError("refs must be a list of at most 12 items")
        changing = set(changing_ids or ())
        normalized = []
        seen_basis = {}
        for item in refs:
            if not isinstance(item, dict) or set(item)!={"card_id","version"}:
                raise ValueError("each ref must be a dict")
            cid = item.get("card_id")
            ver = item.get("version")
            if not self._is_card_id(cid) or not self._is_version(ver):
                raise ValueError("invalid card_id or version")
            if cid in seen_basis:
                if seen_basis[cid] != ver:
                    raise ValueError("conflicting versions for basis")
                continue
            seen_basis[cid] = ver
            normalized.append({"card_id": cid, "version": ver})
        for ref in normalized:
            bid = ref["card_id"]
            if bid == owner_id:
                raise ValueError("self dependency")
            if bid in changing:
                raise ValueError("basis in changing batch")
            row = self._card_row(bid)
            if row is None:
                raise ValueError("unknown basis card")
            if row["state"] != "active":
                raise ValueError("basis not active")
            if row["version"] != ref["version"]:
                raise ValueError("basis version mismatch")
            if project is not None and row["project"] not in (project, "shared"):
                raise ValueError("basis project scope violation")
            if project == "shared" and row["project"] != "shared":
                raise ValueError("shared card may only depend on shared")
        if not self._check_ancestry(owner_id, normalized, changing, {owner_id}, 0):
            raise ValueError("cycle, stale ancestry, or depth exceeded")
        return normalized

    def set(self, card_id, refs):
        self.db.execute(
            "DELETE FROM knowledge_dependencies WHERE card_id = ?", (card_id,)
        )
        for ref in refs:
            self.db.execute(
                "INSERT INTO knowledge_dependencies"
                "(card_id, basis_id, basis_version) VALUES (?,?,?)",
                (card_id, ref["card_id"], ref["version"]),
            )

    def changed(self):
        rows = self.db.execute(
            "SELECT DISTINCT d.card_id AS owner, k.project AS project,"
            " d.basis_id AS basis_id, d.basis_version AS expected,"
            " b.version AS current_version, b.state AS state"
            " FROM knowledge_dependencies d"
            " JOIN knowledge_cards k ON k.id = d.card_id"
            " LEFT JOIN knowledge_cards b ON b.id = d.basis_id"
            " WHERE k.state = 'active'"
            " AND (b.id IS NULL OR b.state != 'active'"
            " OR b.version != d.basis_version)"
            " ORDER BY d.card_id, d.basis_id"
        ).fetchall()
        out = []
        current = None
        for r in rows:
            if current is None or current["card_id"] != r["owner"]:
                current = {
                    "card_id": r["owner"],
                    "project": r["project"],
                    "changed_basis": [],
                }
                out.append(current)
            current["changed_basis"].append(
                {
                    "card_id": r["basis_id"],
                    "expected_version": r["expected"],
                    "current_version": r["current_version"],
                    "state": r["state"],
                }
            )
        return out
