"""Portable immutable project data. No credentials, GUI dependencies or new executor."""

from pathlib import Path
import posixpath
import tempfile
from copy import deepcopy
from types import MappingProxyType

from poster.dispatcher.security import plain_path, private_dir
from poster.dispatcher.snapshots import Snapshots
from poster.maker.actions import default_registry
from poster.maker.model import Graph, graph_secret_refs
from poster.maker.storage import Vault
from poster.maker.variables import has_reference

from .errors import CloudError
from .format import MAX_BYTES, MAX_FILES, canonical, digest, identifier, relative
from poster.portability.blobs import FileBlob, MAX_FILE_BYTES, MAX_BUNDLE_BYTES, copy_blob

PRIVATE_PARTS = {
    "cookies",
    "profiles",
    "profile",
    "private",
    ".git",
    ".poster",
    "credentials",
    "browser-profile",
    "sessions",
}
PRIVATE_SUFFIXES = {
    ".exe",
    ".dll",
    ".py",
    ".sh",
    ".bat",
    ".cmd",
    ".ps1",
    ".js",
    ".pyd",
    ".so",
    ".key",
    ".pem",
    ".sqlite",
    ".db",
    ".enc",
    ".ipynb",
    ".pbackup",
}


def public_resource(name):
    relative(name)
    path = Path(name)
    if (
        any(part.lower() in PRIVATE_PARTS or part.startswith(".") for part in path.parts)
        or path.suffix.lower() in PRIVATE_SUFFIXES
        or any(
            word in path.name.lower()
            for word in ("cookie", "credential", "session.key", "queue.key", "token", "secret", "login data")
        )
    ):
        raise CloudError("PRIVATE_RESOURCE")
    return name


class PortableBundle:
    def __init__(self, manifest, files):
        self._manifest = deepcopy(manifest)
        self.files = MappingProxyType(
            {name: raw if isinstance(raw, FileBlob) else bytes(raw) for name, raw in files.items()}
        )

    @property
    def manifest(self):
        return deepcopy(self._manifest)

    @property
    def version(self):
        return digest(canonical(self._manifest))

    @property
    def project_id(self):
        return self._manifest["project_id"]

    def verify(self):
        try:
            m = self._manifest
            if (
                (m["format"], m["schema_version"])
                not in {("poster.portable.project.v1", 1), ("poster.portable.project.v2", 2)}
                or not isinstance(m["files"], dict)
                or set(self.files) != set(m["files"])
            ):
                raise ValueError()
            identifier(m["project_id"])
            limit = MAX_BYTES if m["schema_version"] == 1 else MAX_BUNDLE_BYTES
            if (
                not 1 <= len(self.files) <= MAX_FILES
                or sum(map(len, self.files.values())) > limit
                or any(len(v) > MAX_FILE_BYTES for v in self.files.values())
            ):
                raise CloudError("PACKAGE_LIMIT")
            if len({name.casefold() for name in self.files}) != len(self.files):
                raise ValueError()
            registry = default_registry()
            refs = set()
            graphs = m["graphs"]
            if (
                not isinstance(graphs, list)
                or m["main"] not in graphs
                or len(set(graphs)) != len(graphs)
                or not set(graphs) <= self.files.keys()
            ):
                raise ValueError()
            for name, raw in self.files.items():
                relative(name)
                if digest(raw) != m["files"][name]:
                    raise ValueError()
                if name in graphs:
                    graph = Graph.model_validate_json(raw)
                    if graph.secret_file or graph.secret_sha256:
                        raise ValueError()
                    for node in graph.nodes:
                        registry.get(node.type, node.action_version)
                    refs.update(graph_secret_refs(graph))
                else:
                    public_resource(name)
            main = Graph.model_validate_json(self.files[m["main"]])
            if main.id != m["project_id"] or sorted(refs) != m["required_secret_refs"]:
                raise ValueError()
            browser = any(
                n.type.startswith("Browser.")
                or n.type == "Logic.If"
                and n.params.get("condition") != "equals"
                for name in graphs
                for n in Graph.model_validate_json(self.files[name]).nodes
            )
            if m["capabilities"] != dict(browser=browser, profiles=False):
                raise ValueError()
            if m.get("result_recipient") is not None:
                from poster.remote.protocol import validate_peer

                validate_peer(m["result_recipient"])
            if main.metadata.get("poster.resources", {}) != {
                n: m["files"][n] for n in self.files if n not in graphs
            }:
                raise ValueError()
            if len(m["outputs"]) > MAX_FILES or len({n.casefold() for n in m["outputs"]}) != len(
                m["outputs"]
            ):
                raise CloudError("PACKAGE_LIMIT")
            for name in m["outputs"]:
                public_resource(name)
            return self
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError("INTEGRITY") from exc

    @classmethod
    def capture(cls, source, *, resources=None, outputs=(), recipient=None, resource_aliases=None):
        source = plain_path(source)
        from poster.maker.http_actions import protect_graph

        with tempfile.TemporaryDirectory(prefix="poster-portable-") as temporary:
            capture = Snapshots(Path(temporary))
            snapshot, captured = capture.capture(source, include_resources=False)
        if resources is None:
            resources = list(
                Graph.model_validate_json(captured[source.name]).metadata.get("poster.resources", {})
            )
        # Publish only reachable graphs and explicitly chosen dependencies.
        pending, reachable = [source.name], set()
        explicit_graphs = {n for n in resources if n.endswith(".postergraph.json")}
        while pending:
            name = relative(pending.pop())
            if name in reachable:
                continue
            if name not in captured:
                raise CloudError("INTEGRITY")
            graph = Graph.model_validate_json(captured[name])
            reachable.add(name)
            for node in graph.nodes:
                if node.type == "Project.Run":
                    child = node.params.get("path", "child.postergraph.json")
                    if has_reference(child):
                        if not explicit_graphs:
                            raise CloudError("CAPABILITY")
                        pending.extend(explicit_graphs)
                    elif isinstance(child, str):
                        pending.append(posixpath.normpath(posixpath.join(posixpath.dirname(name), child)))
                    else:
                        raise CloudError("INTEGRITY")
        files, graph_names, refs = {}, [], set()
        for name, raw in captured.items():
            if name not in reachable:
                continue
            try:
                graph = Graph.model_validate_json(raw)
            except ValueError:
                continue  # Explicitly selected data below, never implicitly copied.
            # Saved Maker graphs already protect credential literals. Reject any
            # unsaved/unprotected credential rather than publishing its value.
            before = graph.model_dump_json()
            protect_graph(graph, Vault())
            if graph.model_dump_json() != before:
                raise CloudError("PRIVATE_RESOURCE")
            graph.secret_file = graph.secret_sha256 = None
            from poster.portability.network import policy

            network_policy = policy(graph.metadata.get("poster.network"))
            graph.metadata = {"poster.network": network_policy} if network_policy else {}
            exports = graph.extensions.get("outputs")
            graph.extensions = {}
            if exports is not None:
                from poster.maker.variables import NAME

                if not isinstance(exports, list) or not all(
                    isinstance(n, str) and NAME.fullmatch(n) for n in exports
                ):
                    raise CloudError("INTEGRITY")
                graph.extensions["outputs"] = exports
            for node in graph.nodes:
                tab = node.extensions.get("browser_tab")
                node.extensions = {}
                if tab:
                    if not isinstance(tab, str) or len(tab) > 200:
                        raise CloudError("INTEGRITY")
                    node.extensions["browser_tab"] = tab
                for key in ("path", "file", "filename", "directory"):
                    value = node.params.get(key)
                    if isinstance(value, str) and value and "{{" not in value:
                        if value in (resource_aliases or {}):
                            target = resource_aliases[value]
                            if target not in resources:
                                raise CloudError("PATH_FORBIDDEN")
                            node.params[key] = value = relative(target)
                        relative(value)
            graph_names.append(relative(name))
            refs.update(graph_secret_refs(graph))
            files[name] = graph.model_dump_json(indent=2).encode()
        selected = {}
        for name in resources:
            if name in files:
                continue
            public_resource(name)
            path = plain_path(source.parent / name)
            if (
                not path.is_relative_to(source.parent)
                or not path.is_file()
                or path.stat().st_size > MAX_FILE_BYTES
            ):
                raise CloudError("PATH_FORBIDDEN")
            if name in files:
                raise CloudError("INTEGRITY")
            selected[name] = (
                FileBlob.capture(path) if path.stat().st_size > 1024 * 1024 else path.read_bytes()
            )
        main = Graph.model_validate_json(files[source.name])
        main.metadata["poster.resources"] = {n: digest(raw) for n, raw in sorted(selected.items())}
        main.metadata["poster.outputs"] = sorted(set(outputs))
        files[source.name] = main.model_dump_json(indent=2).encode()
        files.update(selected)
        large = sum(map(len, files.values())) > MAX_BYTES
        manifest = dict(
            format="poster.portable.project.v2" if large else "poster.portable.project.v1",
            schema_version=2 if large else 1,
            project_id=main.id,
            name=main.name,
            main=source.name,
            graphs=sorted(graph_names),
            files={n: digest(raw) for n, raw in sorted(files.items())},
            outputs=sorted(set(outputs)),
            required_secret_refs=sorted(refs),
            capabilities=dict(
                browser=any(
                    n.type.startswith("Browser.")
                    or n.type == "Logic.If"
                    and n.params.get("condition") != "equals"
                    for g in graph_names
                    for n in Graph.model_validate_json(files[g]).nodes
                ),
                profiles=False,
            ),
            result_recipient=recipient,
        )
        return cls(manifest, files).verify()

    def materialize(self, target):
        self.verify()
        target = private_dir(Path(target))
        if any(target.iterdir()):
            raise CloudError("PATH_FORBIDDEN")
        for name, raw in self.files.items():
            path = plain_path(target / name)
            private_dir(path.parent)
            copy_blob(raw, path)
        return target / self.manifest["main"]


def stage_resources(graph, files, workdir):
    """Each original Worker gets its own copies, never shared writable files."""
    from poster.maker.resources import FileSandbox

    sandbox = FileSandbox(workdir)
    for name, expected in graph.metadata.get("poster.resources", {}).items():
        public_resource(name)
        if name not in files or digest(files[name]) != expected:
            raise CloudError("INTEGRITY")
        copy_blob(files[name], sandbox.path(name))
