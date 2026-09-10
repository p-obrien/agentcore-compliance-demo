SHELL := /usr/bin/env bash

.DEFAULT_GOAL := help

TOFU ?= tofu
AWS_REGION ?= ap-southeast-2
NAME_PREFIX ?= agentcore-compliance-demo
AGENT_IMAGE_FILE ?= agents/.agent-image-uri
AGENT_BUILD_SCRIPT ?= ./agents/build.sh
SEED_RUNNER_PAYLOAD ?= {}
TOFU_INIT_ARGS ?=
TOFU_APPLY_ARGS ?=
TOFU_DESTROY_ARGS ?=

.PHONY: help init deploy destroy

help:
	@printf '%s\n' \
	  'make deploy   Initialize OpenTofu, build and push the ARM64 agent image, apply it by digest, and seed demo data.' \
	  'make destroy  Destroy the stack. Uses the saved image digest when available so AgentCore Gateway cleanup runs.' \
	  'make init     Initialize the OpenTofu working directory.'

init:
	@$(TOFU) init $(TOFU_INIT_ARGS)

deploy: init
	@if [[ -s "$(AGENT_IMAGE_FILE)" ]]; then \
		image_uri="$$(<"$(AGENT_IMAGE_FILE)")"; \
		$(TOFU) apply $(TOFU_APPLY_ARGS) -var "agent_image_uri=$$image_uri"; \
	else \
		$(TOFU) apply $(TOFU_APPLY_ARGS); \
	fi
	@REGION="$(AWS_REGION)" PREFIX="$(NAME_PREFIX)" "$(AGENT_BUILD_SCRIPT)"
	@test -s "$(AGENT_IMAGE_FILE)" || { printf 'Expected a non-empty image URI at %s after the image build.\n' "$(AGENT_IMAGE_FILE)" >&2; exit 1; }
	@image_uri="$$(<"$(AGENT_IMAGE_FILE)")"; \
	$(TOFU) apply $(TOFU_APPLY_ARGS) -var "agent_image_uri=$$image_uri"
	@seed_function_name="$$( $(TOFU) output -raw seed_runner_function_name )"; \
	payload_file="$${TMPDIR:-/tmp}/$${seed_function_name}.json"; \
	function_error="$$(aws lambda invoke --region "$(AWS_REGION)" --function-name "$$seed_function_name" --cli-binary-format raw-in-base64-out --payload '$(SEED_RUNNER_PAYLOAD)' "$$payload_file" --query 'FunctionError' --output text)"; \
	cat "$$payload_file"; printf '\n'; \
	rm -f "$$payload_file"; \
	if [[ "$$function_error" != "None" && "$$function_error" != "null" && -n "$$function_error" ]]; then \
		printf 'Seed runner failed with Lambda FunctionError=%s. Check /aws/lambda/%s in CloudWatch.\n' "$$function_error" "$$seed_function_name" >&2; \
		exit 1; \
	fi

destroy: init
	@if [[ -s "$(AGENT_IMAGE_FILE)" ]]; then \
		image_uri="$$(<"$(AGENT_IMAGE_FILE)")"; \
		$(TOFU) destroy $(TOFU_DESTROY_ARGS) -var "agent_image_uri=$$image_uri"; \
	else \
		printf 'No saved image URI found. Destroying the placeholder-only stack configuration.\n' >&2; \
		$(TOFU) destroy $(TOFU_DESTROY_ARGS); \
	fi
