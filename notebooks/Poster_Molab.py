import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    # Trusted immutable source. No main/HEAD checkout and no embedded credentials.
    import hashlib
    import urllib.request

    _BOOTSTRAP_URL = "https://raw.githubusercontent.com/selventodan-bit/poster/4656d7273af39a257b3874477a8763f20d6ddde6/scripts/cloud/bootstrap.py"
    with urllib.request.urlopen(_BOOTSTRAP_URL, timeout=60) as _response:
        _bootstrap = _response.read(100_000)
    assert (
        hashlib.sha256(_bootstrap).hexdigest()
        == "0453b5764c2c8094b75e9507aaa0c81dc46ea9122dd4e1420a57913974e065b3"
    ), "BOOTSTRAP_INTEGRITY"
    _scope = {"__name__": "poster_cloud_bootstrap"}
    exec(compile(_bootstrap, "verified_poster_bootstrap", "exec"), _scope)
    _scope["install"](
        "4656d7273af39a257b3874477a8763f20d6ddde6",
        "ff7fd2beb77b4beb3d964ecebdf892f4d410ddc76de7e1fa63cbd2ea2964480c",
        "64ed7d46c05c6e408a4a4583b3d7d56e199764e90c88d9fd3489148cc68f18ba",
        molab=True,
    )
    return


@app.cell
def _():
    from poster.cloud.marimo_frontend import MarimoLauncher

    launcher = MarimoLauncher("molab")
    return (launcher,)


@app.cell
def _(launcher):
    launcher.render()
    return


if __name__ == "__main__":
    app.run()
