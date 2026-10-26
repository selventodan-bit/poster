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
        elif args.command == "agent":
            from .agent import Agent

            Agent(args.root).run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(getattr(exc, "code", "REMOTE_CONFIG"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
