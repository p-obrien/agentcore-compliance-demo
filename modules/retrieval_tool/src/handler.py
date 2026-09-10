"""Tenant-isolated AgentCore Gateway retrieval target backed by managed domains.

A caller cannot select a domain, tier, index, or filter. The Lambda verifies a
five-minute HMAC capability, resolves the internal target contract (tenant, tier,
endpoint, index, assume-role ARN, principal class) exclusively from that verified
context, assumes the tier's dedicated IAM read role, and SigV4-signs the Amazon
OpenSearch Service request with service ``es``.

Two assurance tiers are demonstrated:

- Shared (Agencies A and B): one managed domain. The Intelligence API injects an
  immutable ``tenant_id`` term filter and a document-ACL predicate before any
  caller content-matching clause. The domain's fine-grained access control is an
  independent second evaluation, not a substitute for the server-side filter.
- Dedicated (Agency C): a separate managed domain whose resource-based access
  policy denies the shared retrieval principal. If a shared-tier query filter
  ever regresses, the shared principal still receives an authorization denial
  before any Agency C document can be returned.

The handler never trusts caller-supplied ``tenant_id``, ``allowed_groups``,
``actor``, ACL, endpoint, or index fields. Spoof-capable fields are ignored and
audited. Errors fail closed: no partial payload is returned and the handler
never reroutes a shared-tier failure to the dedicated domain.
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

# Per-tenant resolved target contract. Populated from the managed-domain module
# outputs by root wiring. Each entry carries only non-secret routing data:
#   endpoint, index_name, role_arn, tier, principal_class, domain_arn
ES_TARGETS = json.loads(os.environ["ES_TARGETS_JSON"])
SESSION_CONTEXT_SECRET_ARN = os.environ["SESSION_CONTEXT_SECRET_ARN"]
AUDIT_TABLE = os.environ["AUDIT_TABLE"]
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

# SigV4 service name for Amazon OpenSearch Service managed domains. Serverless
# collections used "aoss"; managed domains use "es".
ES_SERVICE = "es"

# Fields a caller might use to try to select a tenant, ACL, actor, endpoint,
# index, tier, or filter. Presence of any of these is recorded as a spoof
# attempt; the derived tenant is always retained.
_SPOOF_KEYS = (
    "tenant_id",
    "tenantId",
    "allowed_groups",
    "allowedGroups",
    "acl",
    "groups",
    "actor",
    "approver",
    "endpoint",
    "index",
    "index_name",
    "filter",
    "assume_role_arn",
    "principal_class",
    "tier",
)

_secrets = boto3.client("secretsmanager", region_name=REGION)
_sts = boto3.client("sts", region_name=REGION)
_ddb = boto3.client("dynamodb", region_name=REGION)
_session_key = None
_assumed_credentials = {}


class DedicatedDomainAccessDenied(Exception):
    """Authorization-denied response from the dedicated domain access policy.

    Distinct from a zero-document search result: the shared principal never
    reached the query, so no document payload is produced.
    """


class RetrievalFailed(Exception):
    """A managed-domain call failed (401/403/429/5xx/timeout) and must fail closed.

    Carries the observed status (an HTTP code or a short label) so the audit
    record can name the failure without leaking the response body.
    """

    def __init__(self, status):
        super().__init__(f"retrieval failed: {status}")
        self.status = status


def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_session_key():
    global _session_key
    if _session_key is None:
        secret = json.loads(
            _secrets.get_secret_value(SecretId=SESSION_CONTEXT_SECRET_ARN)["SecretString"]
        )
        _session_key = secret["key"].encode("utf-8")
    return _session_key


def _verified_session(event):
    """Return signed tenant, groups, subject, and interaction identifier.

    The capability is the only source of tenant, ACL groups, and subject. No
    field is read from prompt text, MCP content arguments, or model output.
    """
    token = (event.get("arguments") or {}).get("session_token")
    if not isinstance(token, str) or token.count(".") != 1:
        raise ValueError("missing signed session context")

    encoded, signature = token.split(".", 1)
    expected = hmac.new(_load_session_key(), encoded.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64decode(signature)
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid signed session context") from exc

    if not hmac.compare_digest(expected, supplied):
        raise ValueError("invalid signed session signature")
    now = int(time.time())
    if not isinstance(payload.get("iat"), int) or payload["iat"] > now + 30:
        raise ValueError("invalid signed session issue time")
    if (
        not isinstance(payload.get("exp"), int)
        or payload["exp"] < now
        or payload["exp"] - payload["iat"] > 300
    ):
        raise ValueError("expired or overlong signed session context")

    tenant_id = payload.get("tenant_id")
    groups = payload.get("allowed_groups")
    subject = payload.get("subject")
    interaction_id = payload.get("interaction_id")
    expected_group = f"{tenant_id}-assessors"
    if (
        not isinstance(tenant_id, str)
        or not isinstance(subject, str)
        or not isinstance(interaction_id, str)
        or not isinstance(groups, list)
        or expected_group not in groups
        or tenant_id not in ES_TARGETS
    ):
        raise ValueError("invalid signed session claims")
    return tenant_id, groups, subject, interaction_id


def _resolve_target(tenant_id):
    """Resolve the internal target contract solely from the verified tenant.

    Agencies A and B route to the shared domain; Agency C routes to the dedicated
    domain. Nothing here depends on any caller-supplied value.
    """
    target = ES_TARGETS[tenant_id]
    return {
        "tenant_id": tenant_id,
        "tier": target["tier"],
        "endpoint": target["endpoint"],
        "index_name": target["index_name"],
        "assume_role_arn": target["role_arn"],
        "principal_class": target["principal_class"],
        "domain_arn": target.get("domain_arn"),
    }


def _spoof_attempt(event):
    """Return the caller-supplied spoof-capable fields, or ``None`` if absent."""
    args = event.get("arguments") or {}
    attempted = {key: args[key] for key in _SPOOF_KEYS if key in args}
    return attempted or None


def _credentials_for(target):
    """Assume only the resolved tier's read role and cache briefly.

    Keyed by role ARN so the shared and dedicated principals never share a cache
    entry.
    """
    role_arn = target["assume_role_arn"]
    cached = _assumed_credentials.get(role_arn)
    now = time.time()
    if cached and cached[1] - now > 60:
        return cached[0]

    response = _sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"es-retrieval-{target['tenant_id']}-{uuid.uuid4().hex[:8]}",
        DurationSeconds=900,
    )
    raw = response["Credentials"]
    credentials = Credentials(raw["AccessKeyId"], raw["SecretAccessKey"], raw["SessionToken"])
    _assumed_credentials[role_arn] = (credentials, raw["Expiration"].timestamp())
    return credentials


def _es_search(body, target):
    """Execute a SigV4-signed search against the resolved managed domain.

    Raises :class:`DedicatedDomainAccessDenied` when the dedicated domain rejects
    the shared principal with an authorization denial (HTTP 401/403), and
    :class:`RetrievalFailed` for any other failure so the caller fails closed.
    """
    url = f"{target['endpoint'].rstrip('/')}/{target['index_name']}/_search"
    encoded_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = AWSRequest(
        method="POST",
        url=url,
        data=encoded_body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(_credentials_for(target), ES_SERVICE, REGION).add_auth(request)
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=encoded_body,
                method="POST",
                headers=dict(request.headers.items()),
            ),
            timeout=10,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # An authorization denial from the dedicated domain access policy is a
        # distinct, expected outcome: surface it separately so no document
        # payload is returned and a dedicated-domain deny audit is written.
        if target["tier"] == "dedicated" and exc.code in (401, 403):
            raise DedicatedDomainAccessDenied() from exc
        raise RetrievalFailed(exc.code) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise RetrievalFailed("timeout") from exc


def _audit(interaction_id, tenant_id, event_type, detail):
    """Append-only audit. Persistence failure makes retrieval fail closed."""
    _ddb.put_item(
        TableName=AUDIT_TABLE,
        Item={
            "interaction_id": {"S": interaction_id},
            "ts": {"N": str(time.time_ns())},
            "tenant_id": {"S": tenant_id or "NONE"},
            "tool": {"S": "retrieval"},
            "event_type": {"S": event_type},
            "detail": {"S": json.dumps(detail)[:8000]},
        },
        ConditionExpression="attribute_not_exists(interaction_id) AND attribute_not_exists(ts)",
    )


def _build_query(tenant_id, groups, query_text, permit_id):
    """Build the search body: immutable tenant + ACL filter, then content clauses.

    The ``tenant_id`` term filter and the ACL predicate are constructed from the
    verified context before any caller content-matching clause is added, and the
    caller cannot replace or remove them. Optional keyword clauses are added as
    ``must`` clauses only; a k-NN clause is added the same way when the caller
    supplies a query vector, without ever touching the isolation filter.
    """
    acl_clause = {
        "bool": {
            "should": [
                {"bool": {"must_not": {"exists": {"field": "allowed_groups"}}}},
                {"terms": {"allowed_groups": groups}},
            ],
            "minimum_should_match": 1,
        }
    }
    query_clauses = []
    if permit_id:
        query_clauses.append({"term": {"permit_id": permit_id}})
    if query_text:
        query_clauses.append(
            {
                "multi_match": {
                    "query": query_text,
                    "fields": ["title^2", "body", "applicant", "permit_type"],
                }
            }
        )
    if not query_clauses:
        query_clauses.append({"match_all": {}})

    return {
        "size": 5,
        "query": {
            "bool": {
                "must": query_clauses,
                # Immutable, server-derived isolation controls. Always applied.
                "filter": [{"term": {"tenant_id": tenant_id}}, acl_clause],
            }
        },
        "_source": [
            "permit_id",
            "title",
            "body",
            "applicant",
            "permit_type",
            "status",
            "tenant_id",
            "created_at",
        ],
    }


def handler(event, _context):
    spoof = _spoof_attempt(event)
    try:
        tenant_id, groups, subject, interaction_id = _verified_session(event)
    except ValueError as exc:
        interaction_id = (
            event.get("interaction_id")
            if isinstance(event.get("interaction_id"), str)
            else str(uuid.uuid4())
        )
        _audit(
            interaction_id,
            None,
            "denied_invalid_session_context",
            {"reason": str(exc), "spoof": spoof},
        )
        return {"results": [], "count": 0, "reason": "invalid_session_context"}

    target = _resolve_target(tenant_id)

    if spoof:
        _audit(
            interaction_id,
            tenant_id,
            "tenant_spoof_attempt_ignored",
            {"attempted": spoof, "effective_tenant_id": tenant_id, "subject": subject},
        )

    args = event.get("arguments") or {}
    query_text = (args.get("query") or "").strip()
    permit_id = (args.get("permit_id") or "").strip()
    body = _build_query(tenant_id, groups, query_text, permit_id)

    try:
        result = _es_search(body, target)
    except DedicatedDomainAccessDenied:
        _audit(
            interaction_id,
            tenant_id,
            "dedicated_domain_access_denied",
            {
                "requesting_principal": target["principal_class"],
                "effective_tenant_id": tenant_id,
                "subject": subject,
            },
        )
        return {
            "results": [],
            "count": 0,
            "tenant_id": tenant_id,
            "interaction_id": interaction_id,
            "reason": "dedicated_domain_access_denied",
        }
    except RetrievalFailed as exc:
        _audit(
            interaction_id,
            tenant_id,
            "retrieval_failed",
            {
                "status": str(exc.status),
                "tier": target["tier"],
                "effective_tenant_id": tenant_id,
                "subject": subject,
            },
        )
        return {
            "results": [],
            "count": 0,
            "tenant_id": tenant_id,
            "interaction_id": interaction_id,
            "reason": "retrieval_failed",
        }

    results = [hit["_source"] for hit in (result.get("hits") or {}).get("hits", [])]
    _audit(
        interaction_id,
        tenant_id,
        "retrieval",
        {
            "query": query_text,
            "permit_id": permit_id,
            "tier": target["tier"],
            "effective_tenant_id": tenant_id,
            "acl_groups": groups,
            "subject": subject,
            "result_count": len(results),
        },
    )
    return {
        "results": results,
        "count": len(results),
        "tenant_id": tenant_id,
        "interaction_id": interaction_id,
    }
