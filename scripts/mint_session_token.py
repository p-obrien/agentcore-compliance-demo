#!/usr/bin/env python3
"""Mint an operator-only signed retrieval request for the rehearsal script.

The HMAC key is read from standard input so it is never placed in a command
argument or repository file. This helper requires AWS access to the restricted
session-context secret and is not an application authentication path.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time
import uuid


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True, choices=("agency-a", "agency-b", "agency-c"))
    parser.add_argument("--query", default="")
    parser.add_argument("--permit-id", default="")
    parser.add_argument("--spoof-tenant")
    args = parser.parse_args()

    secret = json.load(sys.stdin)["key"].encode("utf-8")
    now = int(time.time())
    interaction_id = str(uuid.uuid4())
    payload = {
        "subject": f"operator-rehearsal-{args.tenant}",
        "tenant_id": args.tenant,
        "allowed_groups": [f"{args.tenant}-assessors"],
        "agentcore_tool_scope": [f"{args.tenant}-assessors"],
        "interaction_id": interaction_id,
        "iat": now,
        "exp": now + 300,
    }
    encoded = _b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64url(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    arguments = {"query": args.query, "session_token": f"{encoded}.{signature}"}
    if args.permit_id:
        arguments["permit_id"] = args.permit_id
    if args.spoof_tenant:
        arguments["tenant_id"] = args.spoof_tenant
    print(json.dumps({"arguments": arguments}, separators=(",", ":")))


if __name__ == "__main__":
    main()
