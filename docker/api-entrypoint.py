#!/usr/bin/env python3
"""API container entrypoint: grant socket access, then drop privileges.

The image starts as root so it can join the Docker socket's group (whose gid
is host-specific and cannot be baked in) and open the bind-mounted sequences
folder (host-owned, so the api user could not write it otherwise). It then
permanently drops to the unprivileged ``api`` user before exec'ing the
server: the orchestrator never runs as root, and generated code never runs
in this container at all.
"""

from __future__ import annotations

import os
import pwd
import sys

API_USER = "api"
DOCKER_SOCKET = "/var/run/docker.sock"
SEQUENCES_DIR = "/srv/api/sequences"


def main(argv: list[str]) -> None:
    try:
        api = pwd.getpwnam(API_USER)
    except KeyError:
        print(f"api-entrypoint: user {API_USER!r} not found", file=sys.stderr)
        sys.exit(1)
    groups = [api.pw_gid]
    try:
        sock_gid = os.stat(DOCKER_SOCKET).st_gid
    except OSError:
        sock_gid = None
    if sock_gid is not None and sock_gid not in groups:
        groups.append(sock_gid)
    try:
        os.makedirs(SEQUENCES_DIR, exist_ok=True)
        os.chmod(SEQUENCES_DIR, 0o777)
    except OSError as exc:
        print(
            f"api-entrypoint: cannot prepare {SEQUENCES_DIR}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        os.setgroups(groups)
        os.setgid(api.pw_gid)
        os.setuid(api.pw_uid)
    except OSError as exc:
        print(f"api-entrypoint: privilege drop failed: {exc}", file=sys.stderr)
        sys.exit(1)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: api-entrypoint.py <command> [args...]", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1:])
