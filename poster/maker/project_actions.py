"""Nested graphs reuse GraphRunner and share only host limits and explicit values."""

from pathlib import Path
from typing import Any
from pydantic import Field
from .actions import Action
from .action_fields import properties
from .model import DataModel, GraphError, secret_refs, graph_secret_refs
from .resources import FileSandbox
from .storage import load_graph
from .variables import NAME


class Run(DataModel):
    path: str = Field(default="child.postergraph.json", description="Относительно файла родительского графа")
    inputs: dict[str, Any] = Field(
        default_factory=dict, description="Объявленные входы дочернего проекта; секреты — SecretRef"
    )
    outputs: list[str] = Field(
        default_factory=list,
        max_length=1000,
        description="Подмножество объявленных выходов; [] = все объявленные",
    )


async def run_project(ctx, p):
    from .runtime import GraphRunner

    if len(ctx.ancestry) >= 8:
        raise GraphError("PROJECT_DEPTH")
    base = ctx.project_path.parent if ctx.project_path else ctx.project_root
    child_path = FileSandbox(ctx.project_root).path(str(base / Path(p.path.replace("\\", "/"))))
    if child_path in ctx.ancestry:
        raise GraphError("PROJECT_RECURSION")
    graph, vault = load_graph(child_path)
    # Explicit per-session bindings also resolve protected values in reachable
    # child graphs. Keep the original child runner and its isolated variables.
    for ref in graph_secret_refs(graph):
        if ref not in vault.tokens and ref in ctx.vault.tokens:
            vault.put_value(ctx.vault.get(ref), ref)
    if any(
        n.type.startswith("Browser.") or n.type == "Logic.If" and n.params.get("condition") != "equals"
        for n in graph.nodes
    ):
        raise GraphError("PROJECT_BROWSER_UNSUPPORTED")
    exports = graph.extensions.get("outputs", [])
    if not isinstance(exports, list) or not all(isinstance(n, str) and NAME.fullmatch(n) for n in exports):
        raise GraphError("PROJECT_OUTPUT_INVALID")
    requested = p.outputs or exports
    if any(n not in exports for n in requested):
        raise GraphError("PROJECT_OUTPUT_INVALID")
    vault.env_allowed.update(ctx.vault.env_allowed)
    inputs = {}
    for name, value in p.inputs.items():
        if name not in graph.variables or not graph.variables[name].input:
            raise GraphError("INPUT_UNKNOWN")
        raw = ctx.raw_params.get("inputs", {})
        if graph.variables[name].secret and not secret_refs(raw):
            raise GraphError("PASSWORD_REF_REQUIRED")
        inputs[name] = (
            vault.put_value(value) if ctx.sensitive_action or graph.variables[name].secret else value
        )
    child = GraphRunner(
        graph,
        vault=vault,
        registry=ctx.registry,
        inputs=inputs,
        workdir=ctx.files.root,
        allow_external_files=ctx.files.allow_external,
        project_path=child_path,
        project_root=ctx.project_root,
        budget=ctx.budget,
        ancestry=ctx.ancestry,
        on_event=lambda event: ctx.emit(kind="child", node_id=ctx.current_node, event=event),
    )
    try:
        status = await child.run(breakpoints=False)
        if status == "STOPPED" or ctx.budget.stopped:
            import asyncio

            raise asyncio.CancelledError
        if status != "SUCCEEDED":
            code = next(
                (r.error_code for r in reversed(child.results) if r.error_code), "PROJECT_CHILD_FAILED"
            )
            if code in {"STEP_LIMIT", "RUN_TIMEOUT", "PROJECT_DEPTH", "PROJECT_RECURSION"}:
                raise GraphError(code)
            raise GraphError("PROJECT_CHILD_FAILED", details={"child_code": code})
        result = {}
        for name in requested:
            if name not in child.context.variables.data:
                raise GraphError("PROJECT_OUTPUT_INVALID")
            value = child.context.variables.get(name)
            result[name] = (
                ctx.vault.put_value(child.context.variables.reveal(value)) if secret_refs(value) else value
            )
        return "success", {"outputs": result, "status": "SUCCEEDED"}
    finally:
        await child.close()


def register_project_actions(registry):
    registry.register(
        Action(
            "Project.Run",
            "Проекты · Запустить подпроект",
            Run,
            run_project,
            properties(Run, paths=("path",), secrets=("inputs",)),
            reveal_parameters=True,
        )
    )
