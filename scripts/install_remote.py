"""Reviewed stdlib bootstrap. Verify a release, install pinned hashes, preserve state."""

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import venv


def checked_path(value):
    path = Path(value).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("SYMLINK_PATH")
    if any(c in str(path) for c in '\n\r\x00%"\\'):
        raise ValueError("UNSUPPORTED_PATH")
    return path


def verify(release):
    release = checked_path(release)
    manifest = json.loads((release / "release.json").read_bytes())
    if manifest["format"] != "poster.linux-release.v1" or not re.fullmatch(
        "[0-9a-f]{40}", manifest["commit"]
    ):
        raise ValueError("INVALID_RELEASE")
    if not 1 <= len(manifest["files"]) <= 150:
        raise ValueError("INVALID_RELEASE")
    for name, expected in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("INVALID_RELEASE")
        path = checked_path(release / relative)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("RELEASE_HASH_MISMATCH")
    if "requirements-remote.txt" not in manifest["files"] or manifest["wheel"] not in manifest["files"]:
        raise ValueError("INVALID_RELEASE")
    if not manifest["wheel"].startswith("wheels/") or not manifest["wheel"].endswith(".whl"):
        raise ValueError("INVALID_RELEASE")
    return manifest


def unit(program, state, role, *, user=None):
    program, state = checked_path(program), checked_path(state)
    if role not in {"agent", "relay"} or user is not None and not re.fullmatch("[a-z_][a-z0-9_-]*", user):
        raise ValueError("INVALID_SERVICE")
    identity = ("User=" + user + "\n") if user else ""
    target = "multi-user.target" if user else "default.target"
    return (
        "[Unit]\nDescription=Poster remote "
        + role
        + "\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\n"
        + identity
        + 'ExecStart="'
        + str(program / "current/bin/python")
        + '" -m poster.remote serve --role '
        + role
        + ' --root "'
        + str(state)
        + '"\n'
        + "WorkingDirectory="
        + str(state)
        + '\nUMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nReadWritePaths="'
        + str(state)
        + '"\n'
        + "Restart=on-failure\nRestartSec=3\nKillMode=control-group\nTimeoutStopSec=45\n\n[Install]\nWantedBy="
        + target
        + "\n"
    )


@contextmanager
def stopped(state, role):
    import fcntl

    path = state / ("agent.lock" if role == "agent" else "relay-service.lock")
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("STOP_SERVICE_BEFORE_UPDATE") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def install(release, program, state, role, unit_dir, *, user=None, offline=False):
    if (
        sys.platform != "linux"
        or platform.machine() not in {"x86_64", "AMD64"}
        or sys.version_info[:2] not in {(3, 11), (3, 12), (3, 13)}
    ):
        raise ValueError("REQUIRES_LINUX_X86_64_PYTHON_311_313")
    release, program, state, unit_dir = map(checked_path, (release, program, state, unit_dir))
    if program == state or program in state.parents or state in program.parents:
        raise ValueError("SEPARATE_PROGRAM_AND_STATE")
    manifest = verify(release)
    for path in [program, state, unit_dir]:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    target = checked_path(program / "releases" / manifest["commit"])
    with stopped(state, role):
        if not (target / ".ready").is_file():
            if target.exists():
                raise ValueError("INCOMPLETE_RELEASE_REMOVE_PROGRAM_DIRECTORY_ONLY")
            venv.EnvBuilder(with_pip=True, symlinks=False).create(target)
            python = str(target / "bin/python")
            options = (
                ["--no-index", "--find-links", str(release / "wheelhouse")]
                if offline
                else ["--index-url", "https://pypi.org/simple"]
            )
            # Output can contain local paths/version numbers, never private state/config.
            subprocess.run(
                [
                    python,
                    "-m",
                    "pip",
                    "--isolated",
                    "--disable-pip-version-check",
                    "install",
                    "--only-binary=:all:",
                    *options,
                    "--require-hashes",
                    "-r",
                    str(release / "requirements-remote.txt"),
                ],
                check=True,
            )
            subprocess.run(
                [
                    python,
                    "-m",
                    "pip",
                    "--isolated",
                    "--disable-pip-version-check",
                    "install",
                    "--no-deps",
                    str(release / manifest["wheel"]),
                ],
                check=True,
            )
            subprocess.run([python, "-m", "pip", "check"], check=True)
            (target / ".ready").write_text(manifest["commit"])
        # Existing identity/journal must be readable by the new code BEFORE activation.
        configured = state / ("agent.json" if role == "agent" else "relay.json")
        if configured.exists():
            check = subprocess.run(
                [
                    str(target / "bin/python"),
                    "-m",
                    "poster.remote",
                    "doctor",
                    "--role",
                    role,
                    "--root",
                    str(state),
                ],
                capture_output=True,
                check=False,
            )
            if check.returncode:
                raise ValueError("EXISTING_STATE_INVALID_ACTIVATION_REFUSED")
        link = program / (".activate-" + manifest["commit"])
        link.symlink_to(target, target_is_directory=True)
        link.replace(program / "current")
        destination = unit_dir / ("poster-" + role + ".service")
        with tempfile.NamedTemporaryFile(mode="w", dir=unit_dir, delete=False) as stream:
            stream.write(unit(program, state, role, user=user))
        Path(stream.name).replace(destination)
    return dict(
        commit=manifest["commit"],
        role=role,
        unit=str(destination),
        state_preserved=True,
        configured=configured.exists(),
    )


def manage(action, role, unit_dir, *, system=False):
    if action not in {"start", "stop", "restart", "enable", "disable", "remove", "status"} or role not in {
        "agent",
        "relay",
    }:
        raise ValueError("INVALID_SERVICE_ACTION")
    prefix = ["systemctl"] + ([] if system else ["--user"])
    name = "poster-" + role + ".service"
    if action == "remove":
        subprocess.run([*prefix, "disable", "--now", name], check=True)
        (checked_path(unit_dir) / name).unlink()
        subprocess.run([*prefix, "daemon-reload"], check=True)
    else:
        subprocess.run([*prefix, "daemon-reload"], check=True)
        subprocess.run([*prefix, action, name], check=True)
    return dict(role=role, action=action, state_preserved=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("install")
    add.add_argument("--release", type=Path, required=True)
    add.add_argument("--program", type=Path, required=True)
    add.add_argument("--state", type=Path, required=True)
    add.add_argument("--offline", action="store_true")
    add.add_argument("--service-user")
    control = sub.add_parser("service")
    control.add_argument(
        "--action",
        choices=("start", "stop", "restart", "enable", "disable", "remove", "status"),
        required=True,
    )
    control.add_argument("--system", action="store_true")
    for command in (add, control):
        command.add_argument("--role", choices=("agent", "relay"), required=True)
        command.add_argument("--unit-dir", type=Path, default=Path.home() / ".config/systemd/user")
    args = parser.parse_args()
    try:
        value = (
            install(
                args.release,
                args.program,
                args.state,
                args.role,
                args.unit_dir,
                user=args.service_user,
                offline=args.offline,
            )
            if args.command == "install"
            else manage(args.action, args.role, args.unit_dir, system=args.system)
        )
        print(json.dumps(value))
        return 0
    except Exception as exc:
        code = (
            str(exc)
            if isinstance(exc, ValueError) and re.fullmatch("[A-Z_]+", str(exc))
            else "INSTALL_OR_SERVICE_FAILED"
        )
        print(json.dumps({"code": code, "state_preserved": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
