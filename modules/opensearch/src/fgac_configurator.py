"""Reconcile the OpenSearch Security roles owned by this Terraform module.

The managed-domain endpoints are private. This Lambda runs inside the VPC,
assumes the existing FGAC master role, and applies only the fixed role and
backend-role mappings supplied by Terraform. It never accepts role definitions
or backend roles from the invocation payload.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

REGION = os.environ["AWS_REGION"]
DOMAIN_ADMIN_ROLE_ARN = os.environ["DOMAIN_ADMIN_ROLE_ARN"]
FGAC_SPEC = json.loads(os.environ["FGAC_SPEC_JSON"])
RETRYABLE_STATUSES = (403, 429, 503)
MAX_RETRIES = 5


class FgacReconciliationError(RuntimeError):
    """Raised when a module-owned OpenSearch Security object cannot be applied."""


def _credentials() -> Credentials:
    response = boto3.client("sts", region_name=REGION).assume_role(
        RoleArn=DOMAIN_ADMIN_ROLE_ARN,
        RoleSessionName="opensearch-fgac-reconcile",
        DurationSeconds=900,
    )
    raw = response["Credentials"]
    return Credentials(raw["AccessKeyId"], raw["SecretAccessKey"], raw["SessionToken"])


def _request(credentials: Credentials, endpoint: str, path: str, payload: dict[str, Any]) -> None:
    url = f"{endpoint.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    for attempt in range(MAX_RETRIES + 1):
        request = AWSRequest(
            method="PUT",
            url=url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(credentials, "es", REGION).add_auth(request)
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    url,
                    data=data,
                    method="PUT",
                    headers=dict(request.headers.items()),
                ),
                timeout=20,
            ):
                return
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                time.sleep(min(0.5 * (2**attempt) + random.uniform(0, 0.5), 8.0))
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise FgacReconciliationError(
                f"FGAC PUT {path} failed for {endpoint}: HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise FgacReconciliationError(
                f"FGAC PUT {path} could not reach {endpoint}: {exc.reason}"
            ) from exc


def _reconcile(credentials: Credentials) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for domain in FGAC_SPEC["domains"]:
        endpoint = domain["endpoint"]
        names: list[str] = []
        for role in domain["roles"]:
            name = role["name"]
            encoded_name = urllib.parse.quote(name, safe="")
            _request(
                credentials,
                endpoint,
                f"_plugins/_security/api/roles/{encoded_name}",
                role["definition"],
            )
            _request(
                credentials,
                endpoint,
                f"_plugins/_security/api/rolesmapping/{encoded_name}",
                {"backend_roles": role["backend_roles"]},
            )
            names.append(name)
        applied.append({"endpoint": endpoint, "roles": names})
    return applied


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Apply the static Terraform-owned FGAC specification idempotently."""
    if not isinstance(FGAC_SPEC, dict) or not isinstance(FGAC_SPEC.get("domains"), list):
        raise ValueError("FGAC_SPEC_JSON must contain a domains list")
    return {"applied": _reconcile(_credentials())}
