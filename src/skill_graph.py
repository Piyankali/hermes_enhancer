"""SkillGraph - Skill dependency mapping, traversal, and persistence.

v0.20.7 enterprise: Zero cold-start via SQLite-backed persistence.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple


class SkillGraph:
    """Directed graph for skill dependency mapping with persistence."""

    def __init__(self) -> None:
        self.edges: Dict[str, List[str]] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}

    def register_skill(self, skill: str, depends_on: Optional[List[str]] = None, meta: Optional[Dict[str, Any]] = None) -> None:
        """Register a skill with optional dependencies and metadata."""
        self.edges.setdefault(skill, [])
        if depends_on:
            self.edges[skill].extend(depends_on)
        if meta:
            self.metadata[skill] = meta

    def dependencies(self, skill: str) -> List[str]:
        """Return direct dependencies for a skill."""
        return list(self.edges.get(skill, []))

    def topological_order(self) -> List[str]:
        """Return a topological ordering of registered skills."""
        visited: Set[str] = set()
        order: List[str] = []

        def visit(node: str) -> None:
            if node in visited:
                return
            visited.add(node)
            for dep in self.edges.get(node, []):
                visit(dep)
            order.append(node)

        for node in list(self.edges.keys()):
            visit(node)
        return order

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

    def load_from_db(self, db_conn: sqlite3.Connection) -> None:
        """Hydrate graph from SQLite to eliminate cold-start latency."""
        rows = db_conn.execute("SELECT skill, meta FROM skill_graph_nodes").fetchall()
        for skill, meta_json in rows:
            self.edges.setdefault(skill, [])
            if meta_json:
                try:
                    self.metadata[skill] = json.loads(meta_json)
                except json.JSONDecodeError:
                    self.metadata[skill] = {}

        edges = db_conn.execute("SELECT src, dst FROM skill_graph_edges").fetchall()
        for src, dst in edges:
            self.edges.setdefault(src, [])
            if dst not in self.edges[src]:
                self.edges[src].append(dst)
