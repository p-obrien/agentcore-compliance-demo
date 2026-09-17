#!/usr/bin/env bash
# Build a linux/arm64 image, resolve its immutable ECR digest, and print the
# only image URI accepted by OpenTofu. A mutable tag is never deployed.
set -euo pipefail

REGION="${REGION:-ap-southeast-2}"
PREFIX="${PREFIX:-agentcore-compliance-demo}"
REPO_NAME="${PREFIX}-agents"
TAG="${TAG:-build-$(date -u +%Y%m%d%H%M%S)}"
REQUESTED_ENGINE="${CONTAINER_ENGINE:-}"

fail_engine() {
  cat >&2 <<'EOF'
A supported container build engine is required to build the AgentCore image for
linux/arm64.

Use Docker with Buildx:
  docker buildx version

Or use Podman with platform-aware builds:
  podman build --help

Set CONTAINER_ENGINE=docker or CONTAINER_ENGINE=podman to choose explicitly.
This project does not fall back to `docker build`, because it must push a
linux/arm64 image that AgentCore can run.
EOF
  exit 2
}

select_engine() {
  case "$REQUESTED_ENGINE" in
    "")
      if command -v docker >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; then
        printf '%s\n' "docker"
      elif command -v podman >/dev/null 2>&1 && podman build --help 2>&1 | grep -q -- "--platform"; then
        printf '%s\n' "podman"
      else
        fail_engine
      fi
      ;;
    docker)
      if command -v docker >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; then
        printf '%s\n' "docker"
      else
        echo "CONTAINER_ENGINE=docker requires a usable Docker Buildx installation." >&2
        fail_engine
      fi
      ;;
    podman)
      if command -v podman >/dev/null 2>&1 && podman build --help 2>&1 | grep -q -- "--platform"; then
        printf '%s\n' "podman"
      else
        echo "CONTAINER_ENGINE=podman requires Podman with `podman build --platform` support." >&2
        fail_engine
      fi
      ;;
    *)
      echo "Unsupported CONTAINER_ENGINE=$REQUESTED_ENGINE. Use docker or podman." >&2
      exit 2
      ;;
  esac
}

ENGINE="$(select_engine)"
printf 'Building linux/arm64 image with %s.\n' "$ENGINE"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO_URL="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}"
IMAGE_TAG="${REPO_URL}:${TAG}"

# Ensure the ECR repository exists before pushing. In the `make deploy` flow
# the base `tofu apply` creates it first, so this is normally a no-op. It is a
# safety net for the cases that broke a deploy before: lost/partial state, or
# running this script before the base apply. A missing repo otherwise fails
# the push with "name unknown". Settings match the Terraform resource
# (IMMUTABLE tags, no scan on push), so when the base apply runs afterward it
# sees the repo unchanged. Idempotent: an existing repo is left untouched.
if ! aws ecr describe-repositories \
  --region "$REGION" \
  --repository-names "$REPO_NAME" >/dev/null 2>&1; then
  printf 'ECR repository %s not found; creating it.\n' "$REPO_NAME"
  aws ecr create-repository \
    --region "$REGION" \
    --repository-name "$REPO_NAME" \
    --image-tag-mutability IMMUTABLE \
    --image-scanning-configuration scanOnPush=false >/dev/null
fi

aws ecr get-login-password --region "$REGION" \
  | "$ENGINE" login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

case "$ENGINE" in
  docker)
    # --provenance=false is required. With provenance on, Buildx pushes an OCI
    # image index (image manifest + attestation) and `aws ecr describe-images`
    # returns the *index* digest. AgentCore Runtime expects a single-platform
    # image manifest and rejects the index digest with "The specified image
    # identifier does not exist in the repository." Disabling provenance pushes
    # a plain linux/arm64 image whose digest is the one AgentCore runs.
    docker buildx build \
      --platform linux/arm64 \
      --provenance=false \
      --tag "$IMAGE_TAG" \
      --push \
      "$(dirname "$0")"
    ;;
  podman)
    # AgentCore Runtime resolves a Docker v2 schema 2 manifest
    # (application/vnd.docker.distribution.manifest.v2+json). An OCI manifest
    # (application/vnd.oci.image.manifest.v1+json) is rejected with the generic
    # "The specified image identifier does not exist in the repository." Build
    # and push in Docker format, not OCI.
    podman build \
      --platform linux/arm64 \
      --format docker \
      --tag "$IMAGE_TAG" \
      "$(dirname "$0")"
    podman push --format v2s2 "$IMAGE_TAG"
    ;;
esac

DIGEST="$(aws ecr describe-images \
  --region "$REGION" \
  --repository-name "$REPO_NAME" \
  --image-ids imageTag="$TAG" \
  --query 'imageDetails[0].imageDigest' \
  --output text)"
if [[ ! "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "ECR did not return a valid immutable image digest: $DIGEST" >&2
  exit 1
fi

IMAGE_URI="${REPO_URL}@${DIGEST}"
printf '%s\n' "$IMAGE_URI" > "$(dirname "$0")/.agent-image-uri"
printf 'Image pushed and pinned: %s\n' "$IMAGE_URI"
printf 'Run: tofu apply -var agent_image_uri=%q\n' "$IMAGE_URI"
