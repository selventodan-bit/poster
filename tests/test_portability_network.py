"""Real HTTP/HTTPS/SOCKS5 connections and original GraphRunner isolation."""

import asyncio
import json

import pytest

from poster.maker.example_projects import chain
from poster.maker.runtime import GraphRunner, BrowserSession
from poster.maker.model import GraphError
from poster.maker.fixtures import serve_maker
from poster.portability.network import Network, NetworkProfiles, policy, probe
from portability_proxy_fixture import proxy
from test_cloud_runtime import cloud  # noqa: F401


@pytest.mark.parametrize("kind", ["http", "https", "socks5"])
async def test_actual_proxy_transports_auth_and_tls(kind, tmp_path):
    with serve_maker() as (_, base), proxy(kind, tmp_path) as (server, config):
        assert (await probe(config, base + "/login"))["verified"]
        assert server.hits == 1
        runner = GraphRunner(
            chain("Private connection", [("HTTP.Get", {"url": base + "/login"})]),
            network=config,
            workdir=tmp_path / "work",
        )
        assert await runner.run(breakpoints=False) == "SUCCEEDED"
        assert server.hits == 2
        assert "PROXY_PRIVATE_CANARY" not in repr(Network.parse(config))
        broken = dict(config, password="WRONG")
        with pytest.raises(GraphError) as error:
            await probe(broken, base + "/login")
        assert error.value.code == "NETWORK_AUTH", error.value.code
        if kind == "https":
            with pytest.raises(GraphError, match="NETWORK_TLS"):
                await probe(dict(config, ca=""), base + "/login")


def test_private_profiles_and_project_override(tmp_path):
    profiles = NetworkProfiles(tmp_path / "private")
    selected = profiles.put(
        "Account network",
        dict(kind="http", host="localhost", port=8080, username="poster", password="PROXY_PRIVATE_CANARY"),
    )
    direct = profiles.put("Direct", dict(kind="direct"))
    requested = dict(binding=selected, allow_override=False)
    assert profiles.resolve(requested).kind == "http"
    with pytest.raises(GraphError, match="NETWORK_OVERRIDE"):
        profiles.resolve(requested, override=direct)
    assert profiles.resolve(dict(requested, allow_override=True), override=direct).kind == "direct"
    assert "PROXY_PRIVATE_CANARY" not in json.dumps(profiles.rows())
    assert b"PROXY_PRIVATE_CANARY" not in profiles.secrets.path.read_bytes()
    with pytest.raises(GraphError):
        policy(dict(binding="http://user:password@host", allow_override=True))
    with pytest.raises(GraphError, match="NETWORK_UNSUPPORTED"):
        Network.parse(dict(kind="socks5", host="localhost", port=1080, username="poster")).check(browser=True)
    with pytest.raises(GraphError, match="NETWORK_UNSUPPORTED"):
        Network.parse(dict(kind="http", host="localhost", port=8080)).check(browser=True, provider="qt")
    profiles.delete(selected)
    with pytest.raises(GraphError, match="NETWORK_MISSING"):
        profiles.get(selected)


async def test_real_parallel_browser_networks_have_separate_connections(tmp_path):
    with (
        serve_maker() as (_, base),
        proxy("http", tmp_path / "one") as (one, first),
        proxy("socks5", tmp_path / "two", auth=False) as (two, second),
    ):
        browsers = [BrowserSession(True, network=first), BrowserSession(True, network=second)]
        try:
            await asyncio.gather(*(browser.start() for browser in browsers))
            await asyncio.gather(*(browser.page.goto(base + "/login") for browser in browsers))
            assert one.hits and two.hits
            assert browsers[0].context != browsers[1].context
            for index, browser in enumerate(browsers):
                await browser.context.add_cookies([dict(name="network", value=str(index), url=base)])
            assert [
                next(c["value"] for c in await b.context.cookies() if c["name"] == "network")
                for b in browsers
            ] == ["0", "1"]
        finally:
            await asyncio.gather(*(browser.close() for browser in browsers))


async def test_notebook_private_network_binding_never_enters_source_or_public_result(
    cloud,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from poster.cloud.launcher import Launcher
    from poster.cloud.package import PortableBundle
    from poster.cloud.providers import ProviderConnection
    from poster.cloud.desktop import DesktopCloud
    from poster.maker.storage import save_graph, Vault

    library, api, recipient = cloud
    with serve_maker() as (_, base), proxy("socks5", tmp_path) as (server, config):
        graph = chain("Notebook network", [("HTTP.Get", {"url": base + "/login"})])
        graph.metadata["poster.network"] = dict(binding="company-proxy", allow_override=False)
        source = tmp_path / "network.postergraph.json"
        save_graph(source, graph, Vault())
        bundle = PortableBundle.capture(source, recipient=recipient.public())
        await library.publish(bundle, name=graph.name, recipient=recipient.public())
        monkeypatch.setenv("POSTER_SECRET_PROXY", json.dumps(config))
        backend = Launcher(
            "generic",
            connection=ProviderConnection(
                library, DesktopCloud(tmp_path / "cloud-private", files=library.files)
            ),
            root=tmp_path / "runtime",
        )
        try:
            await backend.connect()
            inspection = await backend.inspect(bundle.project_id, bundle.version)
            assert inspection["bindings"]["Сеть проекта (private JSON)"] == "@network"
            result = await backend.run(
                bundle.project_id,
                bundle.version,
                bindings={"Сеть проекта (private JSON)": "POSTER_SECRET_PROXY"},
            )
            assert result["runs"][0]["status"] == "SUCCEEDED", result
            assert server.hits == 1
            assert "PROXY_PRIVATE_CANARY" not in json.dumps(result)
            assert b"PROXY_PRIVATE_CANARY" not in b"".join(item["raw"] for item in api.files.values())
        finally:
            await backend.close()
