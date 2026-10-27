"""Static compatibility and measured runtime readiness are separate evidence."""

import asyncio
from dataclasses import asdict
import platform
import tempfile
from pathlib import Path

from poster.cloud.capabilities import probe as capacity_probe
from poster.cloud.errors import CloudError
from poster.dispatcher.security import plain_path
from poster.maker.actions import default_registry
from poster.maker.model import graph_secret_refs, GraphError
from poster.maker.storage import load_graph
from .blobs import FileBlob, MAX_BUNDLE_BYTES, capacity
from .network import Network, NetworkProfiles, probe as network_probe
from .resources import dependencies
from .sessions import compatibility

TARGETS = {"windows", "linux", "colab", "molab", "generic"}


async def inspect(
    source,
    target,
    *,
    actual=False,
    provider="chromium",
    available_refs=(),
    profile=None,
    trusted=False,
    network=None,
    probe_url=None,
    remote=None,
    parallel=1,
    session_permission=False,
):
    if target not in TARGETS:
        raise GraphError("PARAMETERS_INVALID")
    source = plain_path(source)
    source_hash = FileBlob.capture(source).sha256
    graph, vault = load_graph(source)
    registry = default_registry()
    static, pending, measured = [], [], []

    def issue(code, **detail):
        static.append(dict(code=code, **detail))

    browser = any(
        n.type.startswith("Browser.") or n.type == "Logic.If" and n.params.get("condition") != "equals"
        for n in graph.nodes
    )
    for node in graph.nodes:
        try:
            registry.get(node.type, node.action_version)
        except GraphError:
            issue("ACTION_UNSUPPORTED", node_id=node.id)
    resource_sizes, total = {}, 0
    for name, expected in graph.metadata.get("poster.resources", {}).items():
        try:
            from poster.cloud.package import public_resource

            public_resource(name)
            blob = FileBlob.capture(source.parent / name)
            if blob.sha256 != expected:
                raise CloudError("INTEGRITY")
            resource_sizes[name] = len(blob)
            total += len(blob)
        except (CloudError, OSError, ValueError):
            issue("RESOURCE_MISSING_OR_CHANGED", resource=name)
    for item in dependencies(graph, resource_sizes, target=target):
        if item["code"] in {"DYNAMIC_DEPENDENCY", "GENERATED_DEPENDENCY"}:
            pending.append(item)
        else:
            static.append(item)
    for node in graph.nodes:
        if node.type in {"Files.ReadText", "CSV.Read", "Excel.Read", "Excel.ListSheets"}:
            name = node.params.get("path")
            limit = 1_000_000 if node.type == "Files.ReadText" else 10_000_000
            if isinstance(name, str) and resource_sizes.get(name, 0) > limit:
                issue("IN_MEMORY_ACTION_LIMIT", node_id=node.id)
    required = graph_secret_refs(graph)
    available = set(available_refs)
    if target == "windows" and platform.system() == "Windows" or target == "generic":
        for ref in vault.tokens:
            try:
                vault.get(ref)
                available.add(ref)
            except GraphError:
                pass
    for ref in sorted(required - available):
        issue("SECRET_REQUIRED", ref=ref)
    profile_report = compatibility(
        profile.data["state"] if profile else {},
        provider,
        "linux-agent" if target == "linux" else target,
        trusted=trusted,
    )
    if profile:
        if (
            not profile_report["supported"]
            or target in {"colab", "molab"}
            or target == "linux"
            and not session_permission
        ):
            issue("PROFILE_INCOMPATIBLE")
        pending.append(dict(code="REAUTHORIZATION_MAY_BE_REQUIRED"))
    network_config = Network()
    try:
        if network is not None:
            network_config = Network.parse(network)
        elif graph.metadata.get("poster.network"):
            if target == "windows" and platform.system() == "Windows" or target == "generic":
                network_config = NetworkProfiles().resolve(graph.metadata["poster.network"])
            else:
                raise GraphError("NETWORK_MISSING")
        network_config.check(browser=browser, provider=provider)
    except GraphError as exc:
        issue(exc.code)
    limit = 1 if target in {"colab", "molab"} else 32
    if type(parallel) is not int or not 1 <= parallel <= limit:
        issue("CAPABILITY")
    if target == "linux" and total > 10_000_000:
        issue("REMOTE_LIMIT")
    if total > MAX_BUNDLE_BYTES:
        issue("PACKAGE_LIMIT")
    external = any(
        n.type.startswith("HTTP.")
        and n.type not in {"HTTP.SetHeader", "HTTP.SetCookie", "HTTP.ClearCookies"}
        or n.type == "Browser.OpenURL"
        for n in graph.nodes
    )
    observed = None
    local = target == "generic" or target == "windows" and platform.system() == "Windows"
    if actual and local:
        observed = asdict(capacity_probe("generic", root=source.parent))
        limit = observed["max_parallel"]
        from poster.maker.runtime import GraphRunner
        from poster.maker.example_projects import chain

        try:
            with tempfile.TemporaryDirectory(prefix="poster-readiness-") as temporary:
                engine = GraphRunner(
                    chain(
                        "Runtime readiness",
                        [("Files.WriteText", dict(path="probe.txt", text="Poster readiness OK"))],
                    ),
                    workdir=temporary,
                )
                try:
                    state = await engine.run(breakpoints=False)
                    if (
                        state != "SUCCEEDED"
                        or (Path(temporary) / "probe.txt").read_text() != "Poster readiness OK"
                    ):
                        raise ValueError()
                    observed["graphrunner_verified"] = True
                finally:
                    await engine.close()
        except Exception:
            measured.append(dict(code="WORKER_UNAVAILABLE"))
        try:
            capacity(source.parent, total * 3 * parallel)
        except CloudError as exc:
            measured.append(dict(code=exc.code))
        if parallel > observed["max_parallel"]:
            measured.append(dict(code="CAPABILITY"))
        if browser:
            if not observed["browser"]:
                measured.append(dict(code="BROWSER_UNAVAILABLE"))
            else:
                from poster.maker.runtime import BrowserSession

                session = BrowserSession(True, provider=provider, network=network_config)
                try:
                    async with asyncio.timeout(20):
                        await session.start()
                        await session.page.goto("about:blank")
                    observed["browser_started"] = True
                except Exception:
                    measured.append(dict(code="BROWSER_UNAVAILABLE"))
                finally:
                    await session.close()
        if external or network_config.kind != "direct":
            if probe_url:
                try:
                    observed["connection"] = await network_probe(network_config, probe_url)
                except GraphError as exc:
                    measured.append(dict(code=exc.code))
            else:
                pending.append(dict(code="EXTERNAL_DEPENDENCIES_UNVERIFIED"))
    elif actual and target == "linux" and remote:
        # Caller supplies authenticated native IPC observations, never metadata alone.
        observed = remote
        limit = remote.get("free_slots", 0)
        if not remote.get("tls_verified") or not remote.get("online"):
            measured.append(dict(code="REMOTE_OFFLINE"))
        if not remote.get("file_returned") or browser and not remote.get("browser_tested"):
            pending.append(dict(code="REMOTE_RUNTIME_NOT_TESTED"))
        if remote.get("codes"):
            measured.extend(dict(code=code) for code in remote["codes"])
        if external:
            pending.append(dict(code="REMOTE_PROJECT_NETWORK_UNVERIFIED"))
    else:
        pending.append(dict(code="RUNTIME_NOT_VERIFIED"))
    if target in {"colab", "molab"}:
        pending.append(dict(code="CLOUD_PROVIDER_NOT_EXECUTED"))
    if FileBlob.capture(source).sha256 != source_hash:
        static.append(dict(code="SOURCE_CHANGED"))
    ready = actual and observed is not None and not static and not measured and not pending
    return dict(
        target=target,
        static_compatible=not static,
        runtime_verified=bool(ready),
        status="READY" if ready else "BLOCKED" if static or measured else "NOT_VERIFIED",
        static_issues=static,
        runtime_issues=measured,
        pending=pending,
        observed=observed,
        registry_actions=len(registry.actions),
        browser_required=browser,
        resource_count=len(resource_sizes),
        resource_bytes=total,
        missing_secret_refs=sorted(required - available),
        profile=profile_report,
        network_kind=network_config.kind,
        max_parallel=limit,
        automatic=target in {"windows", "linux"},
        manual_authorization=bool(profile) or target in {"colab", "molab"},
    )
