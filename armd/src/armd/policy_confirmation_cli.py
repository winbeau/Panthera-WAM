"""Pi-local operator tool for issuing one hardware policy confirmation token."""

from __future__ import annotations

import argparse
import os
import sys

from .policy_confirmation import PolicyConfirmationManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue one short-lived hardware policy confirmation from the Pi's local TTY"
    )
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--socket",
        default=os.environ.get(
            "PANTHERA_POLICY_CONFIRMATION_SOCKET", "/run/user/%d/panthera-policy-confirm.sock" % os.getuid()
        ),
        help="Pi-local Unix socket owned by the armd user",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="explicitly confirm the displayed request/session",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.confirm:
        raise SystemExit("--confirm is required; no token was issued")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("policy confirmation requires a local interactive TTY")
    confirmation = PolicyConfirmationManager.request_local_issue(
        args.socket,
        request_id=args.request_id,
        session_id=args.session_id,
    )
    print(confirmation.confirmation_id)
    print(f"expires_pi_monotonic_ns={confirmation.expires_monotonic_ns}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
