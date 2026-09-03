"""SkillGraph - Skill dependency mapping and traversal."""

from typing import Any, Dict, List, Optional, Set, Tuple


class SkillGraph:
    """Directed graph for skill dependency mapping."""

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
