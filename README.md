# AgentCore Compliance Demo Lab

## The use case

A service team runs one permit-assessment application for several government agencies. Each agency's assessors use an AI assistant to find a permit, read its supporting information, and draft an assessment. The application is shared. The permits are not.

For example, an assessor from Agency A should be able to ask, “Draft an assessment for permit A-1001.” They must not be able to retrieve Agency B's permit `B-2001`, whether they ask directly, alter a tool argument, or include instructions in a retrieved document.

This repository is a working AWS demonstration of that boundary. It deploys the shared application, loads synthetic permits, and provides checks that show what happens when those cross-agency requests are attempted.

### What a successful demo shows

- Agency A can retrieve only Agency A permits, and the same is true for Agencies B and C.
- Agency A gets no result for Agency B permit `B-2001`.
- Sending `tenant_id=agency-b` does not switch the request to Agency B. The audit table records `tenant_spoof_attempt_ignored`.
- Agency C is protected by a separate OpenSearch domain. A shared-tier role receives `401` or `403` before it can read Agency C data.
- The assistant can draft an assessment, but an authenticated approver must approve or reject it before the application writes a permanent record.

The stack runs in AWS Sydney (`ap-southeast-2`) and uses synthetic data from [`seed/documents.json`](seed/documents.json). It creates two billable OpenSearch managed domains. This is a demonstration environment, not a production design or an IRAP assessment. Destroy it after the session.

### Demo data and identities

| Tenant | What the assessor can work with | Storage tier | Demo identity |
|---|---|---|---|
| Agency A | `A-1001` to `A-1003` | Shared OpenSearch domain | `agency-a@example.invalid` |
| Agency B | `B-2001` to `B-2003`, plus the poisoned fixture `B-9999-POISON` | Shared OpenSearch domain | `agency-b@example.invalid` |
| Agency C | `C-3001` to `C-3003` | Dedicated OpenSearch domain | `agency-c@example.invalid` |

Agency A and Agency B use the same private OpenSearch domain. The retrieval Lambda keeps them separate by adding the verified agency and assessor-group filters to every query. Agency C has a separate domain and read role. AWS denies the shared read role at that domain boundary, so Agency C remains inaccessible even if the shared-domain query code is wrong.

The poisoned Agency B fixture contains text that tells the assistant to ignore tenant restrictions and expose `PID-482913`. The assessment agent treats that text as data, applies Bedrock Guardrails, and sends the result to human review. It does not allow the document to change the retrieval scope.

![Tenant-isolation and approval architecture](assets/tenant-isolation-flow.svg)

Read the diagram from left to right: a signed Cognito identity identifies the agency, the retrieval path selects that agency's permitted data, and the approval path prevents a draft assessment from becoming a record without a human decision.

## What this repository deploys

[`main.tf`](main.tf) composes twelve modules. The table names the parts that matter when reading the code or investigating a demo result.

| Module | What it creates or enforces | Key implementation files |
|---|---|---|
| [`modules/identity`](modules/identity) | Cognito user pool, immutable `custom:tenant_id`, assessor groups, tenant-scoped approver groups, agent and approval clients, and the signed-session key | [`modules/identity/main.tf`](modules/identity/main.tf) |
| [`modules/agentcore`](modules/agentcore) | AgentCore runtimes, Gateway, workload identity, and the retrieval Gateway target | [`modules/agentcore/scripts/gateway_target.sh`](modules/agentcore/scripts/gateway_target.sh) |
| [`modules/retrieval_tool`](modules/retrieval_tool) | The in-VPC retrieval Lambda and its IAM policy | [`modules/retrieval_tool/src/handler.py`](modules/retrieval_tool/src/handler.py) |
| [`modules/opensearch`](modules/opensearch) | One shared A/B domain, one dedicated C domain, read roles, seed role, and fine-grained access control | [`modules/opensearch/main.tf`](modules/opensearch/main.tf) |
| [`modules/guardrails`](modules/guardrails) | Bedrock prompt-attack, cross-tenant-topic, PII, and grounding controls | [`modules/guardrails/main.tf`](modules/guardrails/main.tf) |
| [`modules/approval_flow`](modules/approval_flow) | Pending approvals, Step Functions callback workflow, and write-back rules | [`modules/approval_flow/main.tf`](modules/approval_flow/main.tf) |
| [`modules/approval_site`](modules/approval_site) | Cognito-protected approval API and CloudFront-hosted approval page | [`modules/approval_site`](modules/approval_site) |
| [`modules/demo_site`](modules/demo_site) | Cognito-protected demo API and CloudFront-hosted guided walkthrough page, with a relay Lambda and tenant-scoped audit panel | [`modules/demo_site/main.tf`](modules/demo_site/main.tf) |
| [`modules/seed_runner`](modules/seed_runner) | One-shot in-VPC Lambda that loads the synthetic documents during `make deploy` | [`modules/seed_runner/main.tf`](modules/seed_runner/main.tf) |
| [`modules/audit`](modules/audit) | Append-only audit table and read-only trace projection | [`modules/audit/main.tf`](modules/audit/main.tf) |
| [`modules/network`](modules/network) | VPC, private subnets, private OpenSearch security groups, and endpoint or NAT egress | [`modules/network/main.tf`](modules/network/main.tf) |
| [`modules/iam_boundary`](modules/iam_boundary) | Permissions boundary applied to workload roles | [`modules/iam_boundary`](modules/iam_boundary) |

The agent code is in [`agents/intake`](agents/intake) and [`agents/assessment`](agents/assessment). The assessment agent's mandatory review handoff is implemented in [`agents/assessment/agent.py`](agents/assessment/agent.py).

## How a request is enforced

### 1. Cognito establishes the agency

[`modules/identity/main.tf`](modules/identity/main.tf) creates three assessor users and one `demo-operator` user. Each assessor has a non-mutable `custom:tenant_id` value and belongs to one group such as `agency-a-assessors`. The operator belongs to all three `<tenant>-approvers` groups only for this demo.

AgentCore Runtime accepts the Cognito access token. The agent also checks the companion ID token, requires both tokens to name the same subject, and derives the tenant only from those signed claims. The agent client issues 60-minute access and ID tokens.

### 2. AgentCore passes a signed retrieval scope

The runtime creates a five-minute HMAC-signed session capability with the verified subject, tenant, assessor groups, and `interaction_id`. The key lives in the Secret Manager secret created by `modules/identity`; the runtime and retrieval Lambda use it to establish the contract.

[`modules/agentcore/scripts/gateway_target.sh`](modules/agentcore/scripts/gateway_target.sh) registers a `retrieval` tool with `query`, `permit_id`, and the opaque `session_token`. AgentCore's inline schema subset does not support `additionalProperties`, so the Gateway schema is not the tenant boundary. The retrieval Lambda ignores caller-supplied tenant, group, actor, endpoint, index, and filter fields, then audits known scope-spoof attempts. Only the signed session capability determines tenant scope.

### 3. The retrieval Lambda builds an immutable query

[`modules/retrieval_tool/src/handler.py`](modules/retrieval_tool/src/handler.py) verifies the signed capability before it reads `query` or `permit_id`. Its `_build_query` function adds both of these server-derived filters:

```json
{
  "term": { "tenant_id": "<verified tenant>" }
}
```

```json
{
  "terms": { "allowed_groups": ["<verified assessor groups>"] }
}
```

The handler adds caller content matching only as a `must` clause. It cannot remove or replace the tenant and ACL filters. The handler also writes these events to the audit table keyed by `interaction_id` and timestamp:

- `denied_invalid_session_context`
- `tenant_spoof_attempt_ignored`
- `retrieval`
- `retrieval_failed`
- `dedicated_domain_access_denied`

### 4. The OpenSearch target depends on the verified tenant

[`modules/opensearch/main.tf`](modules/opensearch/main.tf) maps `agency-a` and `agency-b` to the shared domain and the `${name_prefix}-os-shared-read` role. It maps `agency-c` to the dedicated domain and the `${name_prefix}-os-dedicated-read` role.

Each read role permits only `es:ESHttpGet` and `es:ESHttpPost` against its own domain. Both roles trust only the retrieval Lambda role. The seed role is separate and adds `es:ESHttpPut` so [`seed/load_seed.py`](seed/load_seed.py) can create the `permits` index and load the fixture data. The OpenSearch operator role is separate from every agent, Gateway, seed, and retrieval role.

Control ownership: OpenSearch and the retrieval Lambda own retrieval filtering and the domain access policies. AgentCore owns identity, tool scope, and session isolation. AgentCore does not replace the retrieval filtering or the OpenSearch access policies; it establishes the verified identity and the signed session that those controls act on. The shared A/B tier is filter-enforced: the retrieval Lambda adds the verified `tenant_id` and group filters to every query. The dedicated Agency C tier is access-policy-enforced: AWS denies the shared read role at the domain boundary before any query runs, plus a document-level `tenant_id = agency-c` filter.

The module applies matching OpenSearch Security backend-role mappings from a private reconciliation Lambda during `tofu apply`. The shared role can search only the shared domain's `permits` index; the dedicated role has the same index restriction plus a fixed `tenant_id = agency-c` document-level security filter. Seed can only check or create `permits`, index a document, and refresh the index. Agencies A and B deliberately share one read role, so their per-agency isolation remains the retrieval Lambda's verified-context `tenant_id` and group filters. Re-run the owned mappings after a manual OpenSearch Security change with:

```bash
tofu apply -replace=module.opensearch.aws_lambda_invocation.fgac \
  -var "agent_image_uri=$(cat agents/.agent-image-uri)"
```

### 5. Assessment is guarded and requires approval

[`agents/assessment/agent.py`](agents/assessment/agent.py) retrieves permits through Gateway, then passes the retrieved text through the Bedrock guardrail before it reaches the Claude model. The guardrail configuration in [`modules/guardrails/main.tf`](modules/guardrails/main.tf) sets high-strength prompt-attack and misconduct filters, denies cross-tenant data-access requests as a topic, anonymizes email, phone, name, and `PID-######` values, and returns grounding and relevance scores.

The assessment agent writes `assessment_review_requested` to audit, starts the state machine, and returns the `interaction_id` and Step Functions execution ARN. The approval flow uses these state transitions:

```text
PENDING_APPROVAL -> DECIDING -> APPROVED | REJECTED | COMMIT_FAILED
```

[`modules/approval_flow/src/write_back/transitions.py`](modules/approval_flow/src/write_back/transitions.py) prevents writes while a proposal remains `PENDING_APPROVAL` or `DECIDING`. A repeated decision returns the stored terminal state instead of starting another write.

## Before you deploy

You need a disposable AWS account, OpenTofu 1.8 or later, AWS CLI v2, [`uv`](https://docs.astral.sh/uv/), and either Docker Buildx or Podman. The configured Claude inference profile, Titan Text Embeddings v2, AgentCore Runtime, and AgentCore Gateway must be available in `ap-southeast-2`.

The agent image must be built and pushed as `linux/arm64`. The build script selects Docker Buildx when available, otherwise it selects Podman. Verify the engine you intend to use before starting `make deploy`:

```bash
# Docker path
docker buildx version

# Podman path
podman version
podman build --help
```

To force Podman, set `CONTAINER_ENGINE=podman`. The project deliberately does not fall back to `docker build`: a host-platform image can be incompatible with the AgentCore runtime.

```bash
export AWS_PROFILE="<your-local-sso-profile>"
aws sso login --profile "$AWS_PROFILE"
```

Copy and review [`terraform.tfvars.example`](terraform.tfvars.example) before applying. Do not commit credentials or an AWS profile name.

### Built-in seed runner

The OpenSearch domains use private VPC endpoints. `make deploy` invokes the stack's `${name_prefix}-seed-runner` Lambda after the digest-pinned AgentCore deployment completes. The function runs in the private application subnets with `seed-sg`, assumes `opensearch_seed_role_arn`, and then signs the index and document requests. A developer laptop never needs direct network access to a managed-domain endpoint.

The runner is intentionally separate from the retrieval Lambda. It has no direct OpenSearch permission, and the retrieval Lambda has no write permission. Both are limited by the workload permissions boundary.

If you change Terraform's `name_prefix`, pass the same value to Make because [`Makefile`](Makefile) uses `NAME_PREFIX` to select the ECR repository for [`agents/build.sh`](agents/build.sh):

```bash
NAME_PREFIX="<matching-name-prefix>" make deploy
```

## Deploy and seed

Run the quick path:

```bash
make deploy
```

`make deploy` runs `tofu init`, applies the base stack (reusing the saved digest on later runs), builds and pushes the ARM64 image, reads its immutable digest from `agents/.agent-image-uri`, applies that digest to create or update AgentCore runtimes and the Gateway target, then invokes the private seed runner. The command checks Lambda's `FunctionError` response and fails if the seed did not finish. Pass `SEED_RUNNER_PAYLOAD='{"no_embeddings":true}'` only when you deliberately want keyword retrieval without vectors.

The loader inserts the ten documents in `seed/documents.json` under their `permit_id` values. It retries only the initialisation responses `403`, `429`, and `503`, at most **5** retries after the initial attempt, using capped exponential backoff with jitter from `0.5` seconds up to a ceiling of `8 seconds`. On exhaustion it reports the permit id, target, final status, and action, then exits non-zero. Reruns update the existing document IDs instead of duplicating them.

Useful outputs:

```bash
tofu output demo_usernames
tofu output demo_passwords                  # sensitive permanent passwords
tofu output demo_page_url
tofu output approval_page_url
tofu output agentcore_gateway_url
tofu output agentcore_runtime_ids
tofu output opensearch_managed_domain_endpoints
tofu output audit_table_name
tofu output trace_read_function_name
```

## Demonstrate the controls

There are three ways to drive the controls: the guided web interface (best for a live training session), the privileged retrieval rehearsals (prove the Lambda's server-side filters without a password), and the focused application-flow scripts. Keep the distinction clear when presenting results.

### Guided web walkthrough

`make deploy` provisions a hosted demo site alongside the stack. Open it and run the whole sequence from a browser, with the audit trail shown live after each step:

```bash
tofu output demo_page_url
```

The page signs an assessor in through the Cognito Hosted UI (authorization code + PKCE) using the agent client, so the browser holds a real per-user access token and companion ID token. It then drives four guided scenarios against the assessment runtime and refreshes a tenant-scoped audit panel after each:

| Scenario | Signed-in as | Request | What the audit panel shows |
|---|---|---|---|
| Happy path | Agency A | `A-1001` | `retrieval` then `assessment_review_requested` for `agency-a` |
| Cross-agency request | Agency A | `permit_id=B-2001` | `retrieval` with count 0; no Agency B content in the draft |
| Tenant spoof | Agency A | prompt says `tenant_id=agency-b` | `tenant_spoof_attempt_ignored`; effective tenant stays `agency-a` |
| Poisoned document | Agency B | `B-9999-POISON` | `guardrail_input` routes the injection to human review |

The third card links to the existing approval page. Sign in there as `demo-operator@example.invalid`, approve or reject, then return and refresh the audit panel to see `approval_decision_authorized` and the proposal's terminal status.

How the browser reaches the runtime, and why it does not weaken isolation: a static page cannot call `bedrock-agentcore:InvokeAgentRuntime` (SigV4, no CORS, no AWS credentials), so a small backend Lambda relays the call. It forwards your two Cognito tokens unchanged (`Authorization: Bearer <access>` and `X-Id-Token: <id>`) and holds no tenant logic. The runtime and retrieval Lambda still derive tenant from the signed claims, exactly as they do for any other client. The demo API's own IAM permits only `InvokeAgentRuntime` on the assessment runtime and `dynamodb:Query` on the audit table, so the web layer can neither reach another runtime nor write the trail. If a reviewer asks whether the web app enforces the boundary, the answer is no, and that is the point.

The audit panel is read-only and scoped to the signed-in assessor's tenant. It derives that tenant from the verified `cognito:groups` claim on the access token, so an Agency A user cannot read Agency B's audit rows even by supplying an `interaction_id` from another agency.

### Privileged retrieval rehearsals

[`scripts/cross_tenant_test.sh`](scripts/cross_tenant_test.sh) reads the deployment's session context secret, creates capabilities with [`scripts/mint_session_token.py`](scripts/mint_session_token.py), and invokes the retrieval Lambda directly. It is not normal user traffic. It proves the Lambda's tenant mapping, role selection, server-side filters, and spoof audit path without requiring a Cognito password in a script.

```bash
./scripts/cross_tenant_test.sh
```

Expected calls and results:

| Call | Expected result |
|---|---|
| Agency A, query `permit` | Only `A-*` permits |
| Agency B, query `permit` | Only `B-*` permits |
| Agency C, query `permit` | Only `C-*` permits from the dedicated tier |
| Agency A, `permit_id=B-2001` | `count = 0` |
| Agency A, spoof `tenant_id=agency-b` | Effective tenant remains `agency-a`; audit event is `tenant_spoof_attempt_ignored` |

[`scripts/dedicated_domain_denial_test.sh`](scripts/dedicated_domain_denial_test.sh) is another privileged rehearsal. It attempts to assume the shared read role and sends a SigV4-signed search directly to the dedicated domain. A `401` or `403` is a pass. The role trust policy normally allows only the retrieval Lambda to assume it, so most deployment operators need an approved diagnostic role path before this script can run.

```bash
./scripts/dedicated_domain_denial_test.sh
```

This direct probe does not write `dedicated_domain_access_denied`; only the application retrieval path writes that audit event after it receives an AWS denial.

### Application flow

The agent client needs both Cognito tokens:

```text
Authorization: Bearer <access-token>
X-Id-Token: <id-token>
```

The repository does not include a login helper or one-command AgentCore Runtime invocation. The demo users have permanent passwords (read them with `tofu output demo_passwords`), so Hosted UI sign-in is a single step with no forced password change. Sign in through the Cognito Hosted UI reachable from `approval_page_url` or `demo_page_url`, then use your approved Cognito client flow to obtain tokens and invoke an Assessment runtime.

A successful assessment returns metadata including `interaction_id`, `retrieved_count`, `grounding_score`, `review_reason`, and `approval_execution_arn`. Sign into the approval page as `demo-operator@example.invalid` to approve or reject the item, then inspect the trace:

```bash
./scripts/verify_trace.sh <INTERACTION_ID>
```

Other focused rehearsals are available for the poisoned fixture, Gateway schema, tool scope, approval lifecycle, and tenant-scoped proposals:

```bash
./scripts/poisoned_document_test.sh
./scripts/runtime_gateway_test.sh
./scripts/out_of_scope_tool_test.sh
./scripts/approval_lifecycle_test.sh
./scripts/tenant_proposal_visibility_test.sh
```

Run [`scripts/check_demo_dependencies.sh`](scripts/check_demo_dependencies.sh) before a presentation. It verifies required OpenTofu outputs exist, but it does not test private DNS, network reachability, TLS, or Cognito authentication. The script may print recording paths as fallbacks; recordings are not stored in this repository.

## Trace a request to code and data

Use this map when an expected result is missing or wrong:

| Symptom | First place to inspect |
|---|---|
| No permits return for any agency | Seed host reachability, `seed/load.sh`, and `seed/documents.json` |
| Wrong agency's permit returns | `modules/retrieval_tool/src/handler.py`, especially `_verified_session`, `_resolve_target`, and `_build_query` |
| Agency C does not deny shared access | `modules/opensearch/main.tf` and `scripts/dedicated_domain_denial_test.sh` |
| Poisoned fixture reaches the model | `agents/assessment/agent.py` and `modules/guardrails/main.tf` |
| Assessment never appears for approval | `agents/assessment/agent.py`, `modules/approval_flow`, and the Step Functions execution ARN |
| Approval does not create a record | `modules/approval_flow/src/write_back/transitions.py` and the proposal's terminal status |
| Audit trace is missing | `modules/audit/main.tf` and `scripts/verify_trace.sh` |

## Tear down

The OpenSearch domains are the ongoing cost. Destroy the stack when you finish:

```bash
make destroy
./scripts/verify_teardown.sh
```

`make destroy` supplies the saved image digest when `agents/.agent-image-uri` exists, which lets AgentCore Gateway target cleanup run. If the image was never built, run `tofu destroy` directly. Destroy the separate [`bootstrap`](bootstrap) state resources too if you created them.

## Limits of this demo

- All users, permits, PII-like values, and agency names are synthetic. This demo uses only synthetic fixtures and does not use any production data.
- This is a demonstration environment. It does not represent a production readiness determination and is not an IRAP assessment.
- `demo-operator@example.invalid` is an approver for all three agencies only to simplify the demonstration. A production design must scope approvers by tenant.
- Gateway target registration uses the AgentCore CLI and stores target IDs under `modules/agentcore/.agentcore`. Preserve OpenTofu state or the target cannot be destroyed automatically.
- Docker Buildx is required for a local ARM64 build. Build the image in another suitable environment if Buildx is not available locally.
- This repository does not settle the real tenant model, identity source, data retention, incident response, model evaluation threshold, customer integration, or multi-account boundary for a production workload.
