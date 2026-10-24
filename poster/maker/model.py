"""Versioned, data-only graph and editor operations. No GUI or browser imports."""

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def identity():
    return uuid.uuid4().hex


class GraphError(ValueError):
    def __init__(self, code, node_id=None, *, details=None):
        self.code, self.node_id = code, node_id
        self.details = details or {}
        super().__init__(code)


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Node(DataModel):
    id: str = Field(default_factory=identity, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    type: str = Field(min_length=1, max_length=100)
    action_version: int = Field(default=1, ge=1, le=1000)
    title: str = Field(default="", max_length=200)
    x: float = Field(default=60, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=60, ge=-1_000_000, le=1_000_000)
    params: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)
    breakpoint: bool = False
    save_as: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
    result_path: str = Field(default="", max_length=200)
    protect_result: bool = False


class Edge(DataModel):
    source: str
    port: str = Field(default="success", pattern=r"^(success|error|case:[0-9]{1,3})$")
    target: str


class View(DataModel):
    zoom: float = Field(default=1, ge=0.25, le=3)


class Variable(DataModel):
    value: Any = None
    input: bool = False
    required: bool = False
    secret: bool = False

    @model_validator(mode="after")
    def protected(self):
        if (
            self.secret
            and self.value is not None
            and not (isinstance(self.value, dict) and set(self.value) == {"secret_ref"})
        ):
            raise ValueError("PASSWORD_REF_REQUIRED")
        json.dumps(self.value, allow_nan=False)
        return self


class Graph(DataModel):
    format: Literal["poster.maker.graph"] = "poster.maker.graph"
    schema_version: Literal[2] = 2
    id: str = Field(default_factory=identity, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(default="Новый проект", min_length=1, max_length=200)
    entry: str | None = None
    nodes: list[Node] = Field(default_factory=list, max_length=2000)
    edges: list[Edge] = Field(default_factory=list, max_length=4000)
    view: View = Field(default_factory=View)
    max_steps: int = Field(default=500, ge=1, le=10000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)
    secret_file: str | None = None
    secret_sha256: str | None = None
    variables: dict[str, Variable] = Field(default_factory=dict, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def migrate(cls, value):
        if isinstance(value, dict) and value.get("schema_version") == 1:
            value = dict(value)
            value["schema_version"] = 2
            value["metadata"] = {**value.get("metadata", {}), "migrated_from": 1}
        return value

    @model_validator(mode="after")
    def topology(self):
        import re

        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) for name in self.variables):
            raise ValueError("VARIABLE_NAME_INVALID")
        nodes = {n.id: n for n in self.nodes}
        for node in self.nodes:
            if node.type == "Logic.Switch" and (
                not isinstance(node.params.get("cases", []), list) or len(node.params.get("cases", [])) > 20
            ):
                raise ValueError("INVALID_CASE_PORT")
        if len(nodes) != len(self.nodes):
            raise ValueError("DUPLICATE_NODE")
        if self.entry is not None and (self.entry not in nodes or nodes[self.entry].type != "Start"):
            raise ValueError("INVALID_ENTRY")
        outputs = set()
        for edge in self.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise ValueError("DANGLING_EDGE")
            if (edge.source, edge.port) in outputs:
                raise ValueError("DUPLICATE_OUTPUT")
            source = nodes[edge.source]
            if source.type in {"End", "End.Success", "End.Failure"} or nodes[edge.target].type == "Start":
                raise ValueError("INVALID_PORT")
            if edge.port.startswith("case:") and (
                source.type != "Logic.Switch" or int(edge.port[5:]) >= len(source.params.get("cases", []))
            ):
                raise ValueError("INVALID_CASE_PORT")
            outputs.add((edge.source, edge.port))
        return self

    def node(self, node_id):
        try:
            return next(n for n in self.nodes if n.id == node_id)
        except StopIteration as exc:
            raise GraphError("NODE_MISSING", node_id) from exc

    def successor(self, node_id, port):
        return next((e.target for e in self.edges if e.source == node_id and e.port == port), None)

    def validate_structure(self):
        return Graph.model_validate(self.model_dump())


def empty_graph():
    start, end = Node(type="Start", x=50, y=60), Node(type="End", x=290, y=60)
    return Graph(entry=start.id, nodes=[start, end], edges=[Edge(source=start.id, target=end.id)])


def secret_refs(value):
    if isinstance(value, dict):
        if set(value) == {"secret_ref"} and isinstance(value["secret_ref"], str):
            return {value["secret_ref"]}
        return set().union(*(secret_refs(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(secret_refs(v) for v in value)) if value else set()
    return set()


def graph_secret_refs(graph):
    return secret_refs([n.params for n in graph.nodes] + [v.value for v in graph.variables.values()])


class Document:
    def __init__(self, graph=None):
        self.graph = graph or empty_graph()
        self.dirty = False
        self.history = []

    def checkpoint(self):
        self.history.append(self.graph.model_dump_json())
        self.history = self.history[-30:]
        self.dirty = True

    def undo(self):
        if self.history:
            self.graph = Graph.model_validate_json(self.history.pop())
            self.dirty = True

    def add(self, action, params, x=80, y=200):
        self.checkpoint()
        node = Node(type=action, params=params, x=x, y=y)
        self.graph.nodes.append(node)
        if action == "Start" and self.graph.entry is None:
            self.graph.entry = node.id
        return node

    def connect(self, source, port, target):
        before = self.graph.model_copy(deep=True)
        self.checkpoint()
        self.graph.edges = [e for e in self.graph.edges if (e.source, e.port) != (source, port)]
        self.graph.edges.append(Edge(source=source, port=port, target=target))
        try:
            self.graph.validate_structure()
        except ValueError as exc:
            self.graph = before
            self.history.pop()
            raise GraphError("INVALID_CONNECTION") from exc

    def delete(self, ids, edge=None):
        self.checkpoint()
        self.graph.nodes = [n for n in self.graph.nodes if n.id not in ids]
        self.graph.edges = [
            e for e in self.graph.edges if e.source not in ids and e.target not in ids and e != edge
        ]
        if self.graph.entry in ids:
            self.graph.entry = None

    def clone(self, ids):
        self.checkpoint()
        mapping = {i: identity() for i in ids}
        nodes = [
            n.model_copy(deep=True, update={"id": mapping[n.id], "x": n.x + 40, "y": n.y + 40})
            for n in self.graph.nodes
            if n.id in ids
        ]
        for node in nodes:
            if (
                node.type in {"Browser.WaitURL", "Browser.WaitPopup"}
                and node.params.get("after_node") in mapping
            ):
                node.params["after_node"] = mapping[node.params["after_node"]]
        edges = [
            e.model_copy(update={"source": mapping[e.source], "target": mapping[e.target]})
            for e in self.graph.edges
            if e.source in ids and e.target in ids
        ]
        self.graph.nodes.extend(nodes)
        self.graph.edges.extend(edges)
        return [n.id for n in nodes]

    def snapshot(self):
        return json.loads(self.graph.model_dump_json())
