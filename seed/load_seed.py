#!/usr/bin/env python3
"""Idempotently seed the two in-VPC OpenSearch Service managed domains.

The loader never receives a database password. It assumes the Terraform-created
seed role, signs requests for the ``es`` service (managed domains, not the
``aoss`` serverless service), creates one ``permits`` index per domain, and
upserts every document by ``permit_id`` as the document ``_id`` so reruns update
in place.

Routing: Agency A and Agency B documents go to the shared domain; Agency C
documents go to the dedicated domain. Each ``tenant_id`` must resolve to exactly
one target.

Bounded retry: only HTTP 403, 429, and 503 are retried, at most five times after
the initial attempt, using capped exponential backoff with jitter. On exhaustion
the loader reports the permit id, domain target, final status, and action, and
exits non-zero.

Required environment variables (set by seed/load.sh):
  SEED_TARGETS_JSON  Terraform output map: tenant -> {endpoint, index_name, tier}
  SEED_ROLE_ARN      IAM role allowed to create indexes and write documents
  AWS_REGION         ap-southeast-2 for this lab
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
EMBED_MODEL = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
SEED_ROLE_ARN = os.environ.get("SEED_ROLE_ARN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
DOCS_PATH = os.path.join(HERE, "documents.json")

# SigV4 service for Amazon OpenSearch Service managed domains.
ES_SERVICE = "es"

# Bounded-retry policy (documented in README and asserted by the unit test).
RETRYABLE_STATUSES = (403, 429, 503)
MAX_RETRIES = 5          # attempts after the initial try
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 8.0

INDEX_MAPPING = {
    "settings": {"index": {"knn": True}},
    "mappings": {
        "properties": {
            "tenant_id": {"type": "keyword"},
            "allowed_groups": {"type": "keyword"},
            "permit_id": {"type": "keyword"},
            "title": {"type": "text"},
            "body": {"type": "text"},
            "applicant": {"type": "text"},
            "permit_type": {"type": "keyword"},
            "status": {"type": "keyword"},
            "created_at": {"type": "date"},
            "is_poisoned": {"type": "boolean"},
            "embedding": {
                "type": "knn_vector",
                "dimension": 1024,
                "method": {
                    "name": "hnsw",
                    "engine": "faiss",
                    "space_type": "l2",
                    "parameters": {"ef_construction": 128, "m": 24},
                },
            },
        }
    },
}


class SeedExhausted(RuntimeError):
    """Raised when a seed operation exhausts its retries.

    Carries the permit id, target endpoint, final status, and action so the
    caller can report exactly which document failed and why.
    """

    def __init__(self, *, permit_id, endpoint, status, action, detail=None):
        message = (
            f"seed operation exhausted retries: permit={permit_id} target={endpoint} "
            f"status={status} action={action}"
        )
        if detail:
            message += f" detail={detail}"
        super().__init__(message)
        self.permit_id = permit_id
        self.endpoint = endpoint
        self.status = status
        self.action = action
        self.detail = detail


def retry_delay(attempt):
    """Capped exponential backoff with jitter for a given retry attempt (1-based).

    Deterministic upper bound: ``min(BASE * 2**(attempt-1), MAX)`` plus up to
    that much jitter, never exceeding ``MAX_DELAY_SECONDS``. Pure and cheap so
    the unit test can assert the schedule matches the documented policy.
    """
    ceiling = min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)
    return min(ceiling + random.uniform(0, ceiling), MAX_DELAY_SECONDS)


def _targets():
    raw = os.environ.get("SEED_TARGETS_JSON", "")
    if not raw:
        raise ValueError("SEED_TARGETS_JSON must be set (run seed/load.sh)")
    targets = json.loads(raw)
    if not isinstance(targets, dict) or not targets:
        raise ValueError("SEED_TARGETS_JSON must be a non-empty target map")
    for tenant, target in targets.items():
        if not isinstance(target, dict) or not target.get("endpoint") or not target.get("index_name"):
            raise ValueError(f"invalid managed-domain target for {tenant}")
    return targets


def _assumed_credentials():
    if not SEED_ROLE_ARN:
        raise ValueError("SEED_ROLE_ARN must be set (run seed/load.sh)")
    response = boto3.client("sts", region_name=REGION).assume_role(
        RoleArn=SEED_ROLE_ARN,
        RoleSessionName="es-lab-seed",
        DurationSeconds=900,
    )
    raw = response["Credentials"]
    return Credentials(raw["AccessKeyId"], raw["SecretAccessKey"], raw["SessionToken"])


def _request(
    credentials,
    method,
    endpoint,
    path,
    payload=None,
    allowed_statuses=(200, 201),
    *,
    permit_id=None,
    sleep=time.sleep,
):
    """Make a signed ``es`` request, retrying only 403/429/503 with backoff.

    Raises :class:`SeedExhausted` after ``MAX_RETRIES`` retries on a retryable
    status, carrying the permit id, target, final status, and action.
    """
    url = f"{endpoint.rstrip('/')}/{path.lstrip('/')}"
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")

    last_status = None
    for attempt in range(MAX_RETRIES + 1):
        request = AWSRequest(
            method=method,
            url=url,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        SigV4Auth(credentials, ES_SERVICE, REGION).add_auth(request)
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    url, data=data, method=method, headers=dict(request.headers.items())
                ),
                timeout=20,
            ) as response:
                body = response.read().decode("utf-8")
                if response.status not in allowed_statuses:
                    raise RuntimeError(
                        f"es {method} {path} returned HTTP {response.status}: {body}"
                    )
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            # A 404 index-existence check arrives here because urllib raises on
            # any non-2xx. An allowed status is a successful outcome, not a
            # failure to retry.
            if exc.code in allowed_statuses:
                body = exc.read().decode("utf-8")
                return exc.code, json.loads(body) if body else {}
            if exc.code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                sleep(retry_delay(attempt + 1))
                continue
            if exc.code in RETRYABLE_STATUSES:
                detail = exc.read().decode("utf-8", errors="replace")
                raise SeedExhausted(
                    permit_id=permit_id or "<index-init>",
                    endpoint=endpoint,
                    status=exc.code,
                    action=f"{method} {path}",
                    detail=detail,
                ) from exc
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"es {method} {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"es {method} {path} connection failed: {exc.reason}") from exc

    raise SeedExhausted(
        permit_id=permit_id or "<index-init>",
        endpoint=endpoint,
        status=last_status,
        action=f"{method} {path}",
    )


def _ensure_index(credentials, target):
    index = target["index_name"]
    endpoint = target["endpoint"]
    status, _ = _request(credentials, "GET", endpoint, index, allowed_statuses=(200, 404))
    if status == 404:
        _request(credentials, "PUT", endpoint, index, INDEX_MAPPING)
        print(f"  created index {index} in {endpoint}")


def _embed(client, text):
    response = client.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}),
    )
    return json.loads(response["body"].read())["embedding"]


def resolve_target(targets, tenant_id):
    """Resolve the single managed-domain target for a document's tenant.

    Raises ``ValueError`` when a tenant has no target, enforcing the
    one-tenant-one-target invariant.
    """
    target = targets.get(tenant_id)
    if target is None:
        raise ValueError(f"No managed-domain target exists for document tenant {tenant_id!r}")
    return target


def run(*, no_embeddings=False):
    """Seed the configured managed domains and return an invocation summary.

    Both the local CLI and the in-VPC Lambda call this function. It raises
    failures to the caller so Lambda returns ``FunctionError`` and the CLI can
    exit non-zero with the same actionable message.
    """
    targets = _targets()
    credentials = _assumed_credentials()

    with open(DOCS_PATH, encoding="utf-8") as file:
        docs = json.load(file)

    # Initialize one permits index per distinct domain endpoint.
    seen_endpoints = set()
    for target in targets.values():
        if target["endpoint"] not in seen_endpoints:
            _ensure_index(credentials, target)
            seen_endpoints.add(target["endpoint"])

    embed_client = boto3.client("bedrock-runtime", region_name=REGION)
    used_endpoints = set()
    for document in docs:
        tenant_id = document.get("tenant_id")
        target = resolve_target(targets, tenant_id)

        indexed = dict(document)
        if not no_embeddings:
            try:
                indexed["embedding"] = _embed(embed_client, f"{document['title']} {document['body']}")
            except Exception as exc:  # Seed keyword retrieval even if Bedrock is unavailable.
                print(f"  embedding failed for {document['permit_id']} ({exc}); loading without vector")

        doc_id = urllib.parse.quote(document["permit_id"], safe="")
        status, _ = _request(
            credentials,
            "PUT",
            target["endpoint"],
            f"{target['index_name']}/_doc/{doc_id}",
            indexed,
            permit_id=document["permit_id"],
        )
        tag = " [POISONED]" if document.get("is_poisoned") else ""
        print(f"  upserted {document['permit_id']} ({tenant_id}) -> {target['tier']} status={status}{tag}")
        used_endpoints.add((target["endpoint"], target["index_name"]))

    for endpoint, index in sorted(used_endpoints):
        _request(credentials, "POST", endpoint, f"{index}/_refresh", {})

    result = {"documents_upserted": len(docs), "index_targets": len(used_endpoints)}
    print(
        f"Done. Upserted {result['documents_upserted']} synthetic documents across "
        f"{result['index_targets']} managed-domain index targets."
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-embeddings", action="store_true", help="Skip Bedrock embedding generation.")
    args = parser.parse_args()

    try:
        run(no_embeddings=args.no_embeddings)
    except (RuntimeError, ValueError, boto3.exceptions.Boto3Error) as exc:
        print(f"SEED FAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
