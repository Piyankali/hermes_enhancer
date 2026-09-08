"""SkillGraph - Skill dependency mapping, traversal, and persistence.

v0.20.7 enterprise: Zero cold-start via SQLite-backed persistence.
v0.23: runtime-usable with cycle detection, controlled errors, skill
name validation (untrusted DB rows are quarantined, never executed),
and explicit registration APIs consumed by the decision engine.
Graph algorithms only -- no ML.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple


SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class CycleError(ValueError):
    """Raised when topological ordering hits a dependency cycle."""

    def __init__(self, cycle: List[str]) -> None:
        self.cycle = cycle
        super().__init__("dependency cycle detected: %s" % " -> ".join(cycle))


def validate_skill_name(skill: Any) -> bool:
    """Controlled skill schema: short, charset-restricted names only."""
    return isinstance(skill, str) and bool(SKILL_NAME_RE.match(skill))


class SkillGraph:
    """Directed graph for skill dependency mapping with persistence."""

    def __init__(self) -> None:
        self.edges: Dict[str, List[str]] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}

    def register_skill(self, skill: str, depends_on: Optional[List[str]] = None, meta: Optional[Dict[str, Any]] = None) -> None:
        """Register a skill with optional dependencies and metadata."""
        if not validate_skill_name(skill):
            raise ValueError("invalid skill name: %r" % (skill,))
        self.edges.setdefault(skill, [])
        for dep in depends_on or []:
            if not validate_skill_name(dep):
                raise ValueError("invalid dependency name: %r" % (dep,))
            if dep not in self.edges[skill]:
                self.edges[skill].append(dep)
        if meta:
            if not isinstance(meta, dict):
                raise ValueError("meta must be a dict")
            self.metadata[skill] = dict(meta)

    def register_dependency(self, skill: str, depends_on: str) -> None:
        """Add a single validated dependency edge."""
        self.register_skill(skill, depends_on=[depends_on])

    def dependencies(self, skill: str) -> List[str]:
        """Return direct dependencies for a skill."""
        return list(self.edges.get(skill, []))

    def topological_order(self) -> List[str]:
        """Return a topological ordering of registered skills.

        Raises CycleError (with the cycle path) instead of recursing
        forever on cyclic graphs.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {}
        order: List[str] = []
        stack: List[str] = []

        def visit(node: str) -> None:
            color[node] = GRAY
            stack.append(node)
            for dep in self.edges.get(node, []):
                state = color.get(dep, WHITE)
                if state == GRAY:
                    cycle = stack[stack.index(dep):] + [dep]
                    raise CycleError(cycle)
                if state == WHITE:
                    visit(dep)
            stack.pop()
            color[node] = BLACK
            order.append(node)

        for node in list(self.edges.keys()):
            if color.get(node, WHITE) == WHITE:
                visit(node)
        return order

    def detect_cycles(self) -> List[List[str]]:
        """Return all dependency cycles (empty list when acyclic)."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {}
        stack: List[str] = []
        cycles: List[List[str]] = []

        def visit(node: str) -> None:
            color[node] = GRAY
            stack.append(node)
            for dep in self.edges.get(node, []):
                state = color.get(dep, WHITE)
                if state == GRAY:
                    cycles.append(stack[stack.index(dep):] + [dep])
                elif state == WHITE:
                    visit(dep)
            stack.pop()
            color[node] = BLACK

        for node in list(self.edges.keys()):
            if color.get(node, WHITE) == WHITE:
                visit(node)
        return cycles

    def dependents(self, skill: str) -> List[str]:
        """Skills that directly depend on *skill* (reverse edges)."""
        return [s for s, deps in self.edges.items() if skill in deps]

    def validate(self) -> Dict[str, Any]:
        """Integrity report: counts, cycles, dangling deps, bad names."""
        bad_names = [s for s in self.edges
                     if not validate_skill_name(s)]
        dangling = sorted({d for deps in self.edges.values() for d in deps
                           if d not in self.edges})
        cycles = self.detect_cycles()
        status = "PASS" if not bad_names and not cycles else "FAIL"
        return {"status": status, "skills": len(self.edges),
                "dependency_count": sum(len(v) for v in self.edges.values()),
                "cycles": cycles, "dangling": dangling,
                "invalid_names": bad_names}

    def summary(self) -> Dict[str, Any]:
        return {
            "skills": list(self.edges.keys()),
            "dependency_count": sum(len(v) for v in self.edges.values()),
        }

    def save_to_db(self, db_conn: sqlite3.Connection) -> None:
        """Persist graph structure to SQLite for fast cold start."""
        now = datetime.now(timezone.utc).isoformat()
        for skill, deps in self.edges.items():
            db_conn.execute(
                "INSERT OR REPLACE INTO skill_graph_nodes (skill, meta, updated_at) VALUES (?, ?, ?)",
                (skill, json.dumps(self.metadata.get(skill, {})), now),
            )
            for dst in deps:
                db_conn.execute(
                    "INSERT OR IGNORE INTO skill_graph_edges (src, dst) VALUES (?, ?)",
                    (skill, dst),
                )
        db_conn.commit()

    def load_from_db(self, db_conn: sqlite3.Connection) -> Dict[str, int]:
        """Hydrate graph from SQLite; quarantine invalid rows.

        Returns {loaded_skills, loaded_edges, quarantined}. Names from
        storage are validated -- invalid entries are skipped, never
        executed or trusted.
        """
        loaded_skills = 0
        loaded_edges = 0
        quarantined = 0
        rows = db_conn.execute("SELECT skill, meta FROM skill_graph_nodes").fetchall()
        for skill, meta_json in rows:
            if not validate_skill_name(skill):
                quarantined += 1
                continue
            self.edges.setdefault(skill, [])
            loaded_skills += 1
            if meta_json:
                try:
                    meta = json.loads(meta_json)
                    if isinstance(meta, dict):
                        self.metadata[skill] = meta
                    else:
                        quarantined += 1
                except (json.JSONDecodeError, TypeError):
                    self.metadata[skill] = {}
                    quarantined += 1

        edges = db_conn.execute("SELECT src, dst FROM skill_graph_edges").fetchall()
        for src, dst in edges:
            if not (validate_skill_name(src) and validate_skill_name(dst)):
                quarantined += 1
                continue
            self.edges.setdefault(src, [])
            if dst not in self.edges[src]:
                self.edges[src].append(dst)
                loaded_edges += 1
        return {"loaded_skills": loaded_skills, "loaded_edges": loaded_edges,
                "quarantined": quarantined}
