"""Public source/wheel CLI; no portable launcher or desktop dependency."""

import json
from pathlib import Path
import signal
import sys

from .models import DispatcherError, MESSAGES


def register(subparsers):
    dispatcher = subparsers.add_parser("dispatcher", help="Durable subprocess queue for saved Maker graphs")
    commands = dispatcher.add_subparsers(dest="dispatcher_command", required=True)
    for name in ["enqueue", "list", "show", "run", "cancel", "stop", "history", "reconcile"]:
        parser = commands.add_parser(name)
        parser.add_argument("--db", type=Path, default=Path(".poster/dispatcher.sqlite"))
        parser.add_argument(
            "--key-file", type=Path, help="Explicit existing Maker/Fernet key file; never the key value"
        )
        if name in {"show", "cancel", "history"}:
            parser.add_argument("job")
        if name == "enqueue":
            parser.add_argument("project", type=Path)
            parser.add_argument("--key", help="Idempotency key; a reused key never resends")
            parser.add_argument("--input", action="append", default=[], metavar="NAME=JSON_OR_TEXT")
            parser.add_argument("--inputs-file", type=Path, help="JSON object of ordinary inputs")
            parser.add_argument(
                "--secrets-file",
                type=Path,
                help="Private JSON object of protected inputs; encrypted on enqueue",
            )
            parser.add_argument("--timeout", type=float, default=300, metavar="SECONDS")
            parser.add_argument("--profile-root", type=Path)
            parser.add_argument(
                "--profile", help="Explicit local profile ID; graph metadata cannot grant access"
            )
        if name == "run":
            parser.add_argument("--once", action="store_true")
            parser.add_argument("--max-workers", type=int, default=2)
            parser.add_argument("--max-per-project", type=int, default=1)
        if name == "list":
            parser.add_argument("--limit", type=int, default=100)
        if name == "show":
            parser.add_argument(
                "--report", action="store_true", help="Include the latest verified, payload-free report"
            )


def read_inputs(path):
    from .security import plain_path

    try:
        path = plain_path(path)
        with path.open("rb") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception as exc:
        raise DispatcherError("INPUT_INVALID") from exc


def execute(args):
    from .service import Dispatcher, enqueue
    from .store import Store

    if getattr(sys, "frozen", False):
        raise DispatcherError("PROCESS_CONTROL_FAILED")
    store = Store(args.db)
    try:
        command = args.dispatcher_command
        if command == "enqueue":
            inputs = read_inputs(args.inputs_file) if args.inputs_file else {}
            for item in args.input:
                name, separator, raw = item.partition("=")
                if not separator:
                    raise DispatcherError("INPUT_INVALID")
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = raw
                inputs[name] = value
            job = enqueue(
                store,
                args.project,
                inputs=inputs,
                secrets=read_inputs(args.secrets_file) if args.secrets_file else {},
                key=args.key,
                timeout=args.timeout,
                profile_root=args.profile_root,
                profile_id=args.profile,
                key_file=args.key_file,
            )
            return store.show(job), 0
        if command == "list":
            return {"jobs": store.rows(args.limit), "counts": store.counts()}, 0
        if command == "show":
            job = store.show(args.job)
            if args.report and job["history"]:
                latest = job["history"][0]
                dispatcher = Dispatcher(store)
                if not dispatcher.validate_confirmation(latest):
                    raise DispatcherError("REPORT_INVALID")
                job["report"] = json.loads(Path(latest["report"]).read_bytes())
            return job, 0
        if command == "history":
            store.payload(args.job)
            return {"job_id": args.job, "executions": store.history(args.job)}, 0
        if command == "cancel":
            return store.cancel(args.job), 0
        if command == "stop":
            store.request_stop()
            return {"status": "STOP_REQUESTED"}, 0
        dispatcher = Dispatcher(
            store,
            key_file=args.key_file,
            **(
                {"max_workers": args.max_workers, "max_per_project": args.max_per_project}
                if command == "run"
                else {}
            ),
        )
        if command == "reconcile":
            return dispatcher.reconcile(), 0
        previous = {}

        def stop(signum, frame):
            if dispatcher.stop_requested:
                raise KeyboardInterrupt
            dispatcher.stop_requested = True

        try:
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                if hasattr(signal, name):
                    sig = getattr(signal, name)
                    previous[sig] = signal.signal(sig, stop)
            result = dispatcher.run(once=args.once)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        failed = store.db.execute(
            "SELECT count(*) FROM executions WHERE owner=? AND status!='SUCCEEDED'", (dispatcher.owner,)
        ).fetchone()[0]
        return result, int(bool(failed))
    finally:
        store.close()


def main(args):
    try:
        result, code = execute(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        code = exc.code if isinstance(exc, DispatcherError) else "EXECUTION_FAILED"
        print(json.dumps({"status": "ERROR", "code": code, "message": MESSAGES[code]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code
