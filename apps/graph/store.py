"""Graph stores: where projected nodes and edges are written.

Projectors (``projection.py``) describe changes as four idempotent operations:

* ``node(label, key, props, extra_labels)``  - create or update a node, identified by label + key
* ``edge(from, type, to, props, merge_on)``   - create or update a relationship between two nodes
* ``clear_edges(label, key, type)``           - drop a node's outgoing relationships of one type
* ``delete_node(label, key)``                 - remove a node and its relationships

``Neo4jStore`` turns them into Cypher ``MERGE`` statements and runs them in one transaction per
``flush()``. ``MemoryStore`` keeps them in Python dictionaries so the projection rules can be tested
without a Neo4j server.

Labels, relationship types and property names cannot be Cypher parameters, so they are checked
against a strict pattern before they reach a query; all values are passed as parameters.
"""
import re
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,59}$")


def _name(value: str) -> str:
    if not IDENTIFIER.match(value):
        raise ValueError(f"{value!r} is not a valid graph label, relationship type or property name.")
    return value


def clean(props: dict) -> dict:
    """Neo4j has no decimal type: money and quantities are stored as floats; dates stay dates."""
    result = {}
    for name, value in props.items():
        _name(name)
        if isinstance(value, Decimal):
            value = float(value)
        elif value is not None and not isinstance(value, (str, int, float, bool, date, datetime, list)):
            value = str(value)
        result[name] = value
    return result


class MemoryStore:
    """In-memory graph with the same operations, for tests and dry runs."""

    def __init__(self):
        self.nodes = {}  # (label, key) -> {"labels": set, "props": dict}
        self.edges = {}  # (from_label, from_key, type, to_label, to_key, merge_on items) -> props

    def node(self, label, key, props=None, extra_labels=()):
        entry = self.nodes.setdefault((_name(label), key), {"labels": {label}, "props": {"key": key}})
        entry["labels"].update(_name(extra) for extra in extra_labels)
        for name, value in clean(props or {}).items():
            if value is None:
                entry["props"].pop(name, None)
            else:
                entry["props"][name] = value

    def edge(self, from_label, from_key, rel_type, to_label, to_key, props=None, merge_on=None):
        self.node(from_label, from_key)
        self.node(to_label, to_key)
        identity = tuple(sorted(clean(merge_on or {}).items()))
        key = (from_label, from_key, _name(rel_type), to_label, to_key, identity)
        self.edges.setdefault(key, dict(identity)).update(clean(props or {}))

    def clear_edges(self, label, key, rel_type):
        for edge_key in [k for k in self.edges if k[0] == label and k[1] == key and k[2] == rel_type]:
            del self.edges[edge_key]

    def delete_node(self, label, key):
        self.nodes.pop((label, key), None)
        for edge_key in [k for k in self.edges if (k[0], k[1]) == (label, key) or (k[3], k[4]) == (label, key)]:
            del self.edges[edge_key]

    def flush(self):
        """Nothing to send: changes are applied immediately."""

    def clear(self):
        self.nodes.clear()
        self.edges.clear()

    def ensure_schema(self, labels):
        """No constraints to create in memory."""

    # --- helpers for tests ----------------------------------------------------------------
    def props(self, label, key):
        """Test helper: the properties of one node."""
        return self.nodes[(label, key)]["props"]

    def labels(self, label, key):
        """Test helper: the labels of one node."""
        return self.nodes[(label, key)]["labels"]

    def related(self, label, key, rel_type):
        """Outgoing relationships of one type: [(to_label, to_key, props)]."""
        return sorted(((k[3], k[4], props) for k, props in self.edges.items()
                       if k[0] == label and k[1] == key and k[2] == rel_type), key=lambda e: (e[0], e[1]))


class Neo4jStore:
    """Writes to Neo4j. Operations are buffered and sent in one transaction by ``flush()``."""

    def __init__(self, uri=None, user=None, password=None):
        from neo4j import GraphDatabase  # imported here so the rest of the app runs without the driver

        self.driver = GraphDatabase.driver(uri or settings.NEO4J_URI,
                                           auth=(user or settings.NEO4J_USER, password or settings.NEO4J_PASSWORD))
        self.pending = []

    def close(self):
        """Close the Neo4j driver and its connection pool."""
        self.driver.close()

    def node(self, label, key, props=None, extra_labels=()):
        extra = "".join(f" SET n:`{_name(name)}`" for name in extra_labels)
        self.pending.append((f"MERGE (n:`{_name(label)}` {{key: $key}}) SET n += $props{extra}",
                             {"key": key, "props": clean(props or {})}))

    def edge(self, from_label, from_key, rel_type, to_label, to_key, props=None, merge_on=None):
        merge_on = clean(merge_on or {})
        identity = ", ".join(f"{name}: $m_{name}" for name in merge_on)
        identity = f" {{{identity}}}" if identity else ""
        cypher = (f"MERGE (a:`{_name(from_label)}` {{key: $a}}) MERGE (b:`{_name(to_label)}` {{key: $b}}) "
                  f"MERGE (a)-[r:`{_name(rel_type)}`{identity}]->(b) SET r += $props")
        params = {"a": from_key, "b": to_key, "props": clean(props or {}),
                  **{f"m_{name}": value for name, value in merge_on.items()}}
        self.pending.append((cypher, params))

    def clear_edges(self, label, key, rel_type):
        self.pending.append((f"MATCH (:`{_name(label)}` {{key: $key}})-[r:`{_name(rel_type)}`]->() DELETE r",
                             {"key": key}))

    def delete_node(self, label, key):
        self.pending.append((f"MATCH (n:`{_name(label)}` {{key: $key}}) DETACH DELETE n", {"key": key}))

    def flush(self):
        statements, self.pending = self.pending, []
        if not statements:
            return

        def write(tx):
            for cypher, params in statements:
                tx.run(cypher, params).consume()

        with self.driver.session() as session:
            session.execute_write(write)

    def clear(self):
        """Delete every node and relationship (used by ``graph_rebuild --clear`` and tests)."""
        self.pending = []
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()

    def ensure_schema(self, labels):
        """A uniqueness constraint on ``key`` per label: fast lookups and no duplicate nodes."""
        with self.driver.session() as session:
            for label in labels:
                session.run(f"CREATE CONSTRAINT {_name(label).lower()}_key IF NOT EXISTS "
                            f"FOR (n:`{label}`) REQUIRE n.key IS UNIQUE").consume()

    def read(self, cypher, params=None):
        """Run a read query and return plain Python values (dates as ISO strings)."""
        with self.driver.session() as session:
            return [{k: _native(v) for k, v in record.items()}
                    for record in session.execute_read(lambda tx: list(tx.run(cypher, params or {})))]


def _native(value):
    if hasattr(value, "iso_format"):  # neo4j.time.Date / DateTime
        return value.iso_format()
    if isinstance(value, list):
        return [_native(item) for item in value]
    if isinstance(value, dict):
        return {k: _native(v) for k, v in value.items()}
    if hasattr(value, "labels") and hasattr(value, "items"):  # a Node
        return {"labels": sorted(value.labels), **{k: _native(v) for k, v in value.items()}}
    return value


_store = None


def get_store():
    """The shared Neo4j store (created on first use)."""
    global _store
    if _store is None:
        _store = Neo4jStore()
    return _store
