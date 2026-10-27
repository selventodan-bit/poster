import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    # Trusted immutable source. No main/HEAD checkout and no embedded credentials.
    import hashlib
    import urllib.request

    _BOOTSTRAP_URL = "https://raw.githubusercontent.com/selventodan-bit/poster/e828f6c60010d71612e69681ce340eb8a0aeacc7/scripts/cloud/bootstrap.py"
    with urllib.request.urlopen(_BOOTSTRAP_URL, timeout=60) as _response:
        _bootstrap = _response.read(100_000)
    assert (
        hashlib.sha256(_bootstrap).hexdigest()
        == "0453b5764c2c8094b75e9507aaa0c81dc46ea9122dd4e1420a57913974e065b3"
    ), "BOOTSTRAP_INTEGRITY"
    _scope = {"__name__": "poster_cloud_bootstrap"}
    exec(compile(_bootstrap, "verified_poster_bootstrap", "exec"), _scope)
    _scope["install"](
        "e828f6c60010d71612e69681ce340eb8a0aeacc7",
        "89bbb4e8755a8e47fce7049877570ea052acd9b2a03f33597796e5ced37bdbfc",
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
