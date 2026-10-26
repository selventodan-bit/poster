"""Provision by private file; run relay or Linux agent. Never accept credentials in argv."""

import argparse
import os
from pathlib import Path

from .registry import read_json


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    tls = sub.add_parser("tls-init")
    tls.add_argument("--root", type=Path, required=True)
    tls.add_argument("--hostname", default="localhost")
    init = sub.add_parser("relay-init")
    init.add_argument("--root", type=Path, required=True)
    init.add_argument("--url", required=True)
    init.add_argument("--ca", type=Path)
    relay = sub.add_parser("relay")
    relay.add_argument("--config", type=Path, required=True)
    relay.add_argument("--database", type=Path, required=True)
    relay.add_argument("--cert", type=Path, required=True)
    relay.add_argument("--key-file", type=Path, required=True)
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=8443)
    init = sub.add_parser("agent-init")
    init.add_argument("--root", type=Path, required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--controller", type=Path, required=True)
    init.add_argument("--relay-profile", type=Path, required=True)
    agent = sub.add_parser("agent")
    agent.add_argument("--root", type=Path, required=True)
    pair = sub.add_parser("pair")
    pair.add_argument("--root", type=Path, required=True)
    pair.add_argument("--name", required=True)
    pair.add_argument("--offer", type=Path, required=True)
    pair.add_argument("--expect-controller", required=True)
    for name in ("doctor", "status", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--role", choices=("agent", "relay"), required=True)
        if name == "doctor":
            command.add_argument("--network", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "tls-init":
            from .setup import tls_files

            tls_files(args.root, args.hostname)
        elif args.command == "relay-init":
            from .setup import relay_profiles

            relay_profiles(args.root, args.url, args.ca)
        elif args.command == "relay":
            from .relay import server

            service = server(
                read_json(args.config),
                args.database,
                args.cert,
                args.key_file,
                host=args.host,
                port=args.port,
            )
            try:
                service.serve_forever()
            finally:
                service.server_close()
        elif args.command == "agent-init":
            from .setup import agent_init

            agent_init(args.root, args.name, args.controller, args.relay_profile)
        elif args.command == "pair":
            from .pairing import agent_pair

            agent_pair(args.root, args.name, args.offer, args.expect_controller)
        elif args.command in {"doctor", "status"}:
            import json
            from .diagnostics import doctor
            from .service import status

            value = (
                status(args.root, args.role)
                if args.command == "status"
                else doctor(args.root, args.role, network=args.network)
            )
            print(json.dumps(value))
            return 0 if value["ready"] else 2
        elif args.command == "serve":
            from .service import serve

            serve(args.root, args.role)
        elif args.command == "agent":
            from .agent import Agent
            import signal

            agent = Agent(args.root)
            signal.signal(signal.SIGTERM, lambda *_: setattr(agent, "done", True))
            agent.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(getattr(exc, "code", "REMOTE_CONFIG"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
