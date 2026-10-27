import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    # Trusted immutable source. No main/HEAD checkout and no embedded credentials.
    import hashlib
    import urllib.request

    _BOOTSTRAP_URL = "https://raw.githubusercontent.com/selventodan-bit/poster/830b6024f23f8625e4d0871a0322c4c73cf473ad/scripts/cloud/bootstrap.py"
    with urllib.request.urlopen(_BOOTSTRAP_URL, timeout=60) as _response:
        _bootstrap = _response.read(100_000)
    assert (
        hashlib.sha256(_bootstrap).hexdigest()
        == "0453b5764c2c8094b75e9507aaa0c81dc46ea9122dd4e1420a57913974e065b3"
    ), "BOOTSTRAP_INTEGRITY"
    _scope = {"__name__": "poster_cloud_bootstrap"}
    exec(compile(_bootstrap, "verified_poster_bootstrap", "exec"), _scope)
    _scope["install"](
        "830b6024f23f8625e4d0871a0322c4c73cf473ad",
        "c388fb6640fc8eddc8c8e283ed8a85f0c8a7e95e9ac6f8adb9effdf20dbac9a9",
        "5a16453ddd0b38119f1f4ee64284164591c2c86d904cb37351016bd323501fbc",
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
