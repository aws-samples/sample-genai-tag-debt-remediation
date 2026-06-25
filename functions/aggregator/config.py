# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""TagSense shared configuration.

Centralizes environment variable loading, defaults, and tag policy management
for all Lambda functions in the TagSense pipeline.
"""

import os
import json
import logging

import boto3

logger = logging.getLogger(__name__)

# --- S3 Storage ---
RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "")
RESULTS_PREFIX = os.environ.get("RESULTS_PREFIX", "tagsense")

# --- Model Configuration ---
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
# Batch inference model ID. Per AWS docs, newer Claude models (Sonnet 4, 4.6, Haiku 4.5)
# support batch ONLY via cross-region inference profiles (us. prefix).
# Defaults to same model as real-time. Set empty to skip batch and always use real-time.
BEDROCK_BATCH_MODEL_ID = os.environ.get(
    "BEDROCK_BATCH_MODEL_ID", os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
)

# --- Notifications ---
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

# --- Region ---
REGION = os.environ.get("AWS_REGION", "us-east-1")

# --- Inference Thresholds ---
# Minimum fraction of VPC peers that must agree for neighbor consensus (Tier 3)
NEIGHBOR_CONSENSUS_THRESHOLD = float(
    os.environ.get("NEIGHBOR_CONSENSUS_THRESHOLD", "0.7")
)
# Minimum confidence score to accept a Bedrock AI suggestion (Tier 4)
BEDROCK_CONFIDENCE_THRESHOLD = int(
    os.environ.get("BEDROCK_CONFIDENCE_THRESHOLD", "50")
)
# How far back to search CloudTrail for resource creation events (Tier 2)
CLOUDTRAIL_LOOKBACK_DAYS = int(os.environ.get("CLOUDTRAIL_LOOKBACK_DAYS", "90"))
# Days of zero usage before flagging as orphan candidate (Tier 5)
ORPHAN_INACTIVITY_DAYS = int(os.environ.get("ORPHAN_INACTIVITY_DAYS", "30"))

# --- Processing Limits (configurable per deployment) ---
MAX_RESOURCES = int(os.environ.get("MAX_RESOURCES", "10000"))
MAX_BEDROCK_CALLS = int(os.environ.get("MAX_BEDROCK_CALLS", "500"))

# --- Default Tag Policy ---
# Used when TAG_POLICY environment variable is not set.
# In production, supply TAG_POLICY as JSON or use Organizations DescribeEffectivePolicy.
DEFAULT_TAG_POLICY = {
    "Owner": {"required": True, "description": "Team or individual owning the resource"},
    "Environment": {
        "required": True,
        "allowed_values": ["prod", "staging", "dev", "sandbox"],
    },
    "CostCenter": {"required": True, "description": "Budget code for cost allocation"},
    "Application": {"required": True, "description": "Application or workload name"},
}

# Maps resource types to their CloudTrail creation event names.
# Used by Tier 2 to find the creator of a resource.
CREATE_EVENT_MAP = {
    "ec2:instance": "RunInstances",
    "s3:bucket": "CreateBucket",
    "rds:db": "CreateDBInstance",
    "lambda:function": "CreateFunction20150331",
    "dynamodb:table": "CreateTable",
    "sqs:queue": "CreateQueue",
    "sns:topic": "CreateTopic",
    "ecs:cluster": "CreateCluster",
    "elasticloadbalancing:loadbalancer": "CreateLoadBalancer",
}


def load_tag_policy() -> dict:
    """Load tag policy from environment, Organizations API, or return default.

    Priority:
        1. TAG_POLICY env var (JSON string) — explicit override
        2. AWS Organizations effective tag policy (if in an org with tag policies)
        3. DEFAULT_TAG_POLICY constant

    Returns:
        dict: Tag policy mapping tag keys to their configuration
              (required, allowed_values, description).
    """
    # Priority 1: Explicit env var override
    policy_json = os.environ.get("TAG_POLICY")
    if policy_json:
        try:
            return json.loads(policy_json)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in TAG_POLICY env var, trying Organizations API")

    # Priority 2: Pull from AWS Organizations effective tag policy
    try:
        org = boto3.client("organizations")
        resp = org.describe_effective_policy(
            PolicyType="TAG_POLICY", TargetId=_get_account_id()
        )
        org_policy = json.loads(resp["EffectivePolicy"]["PolicyContent"])
        parsed = _parse_org_tag_policy(org_policy)
        if parsed:
            logger.info("Loaded tag policy from Organizations API (%d keys)", len(parsed))
            return parsed
    except Exception as e:
        # Expected if not in an org or tag policies not enabled
        logger.debug("Organizations tag policy not available: %s", e)

    # Priority 3: Default
    return DEFAULT_TAG_POLICY


def _get_account_id() -> str:
    """Get current account ID for Organizations API call."""
    try:
        return boto3.client("sts").get_caller_identity()["Account"]
    except Exception:
        return ""


def _parse_org_tag_policy(org_policy: dict) -> dict:
    """Convert AWS Organizations tag policy format to TagSense internal format.

    Org format: {"tags": {"Environment": {"tag_key": {"@@assign": "Environment"},
                 "tag_value": {"@@assign": ["prod", "dev"]}}}}
    TagSense format: {"Environment": {"required": True, "allowed_values": ["prod", "dev"]}}
    """
    result = {}
    for key, config in org_policy.get("tags", {}).items():
        entry = {"required": True}  # If it's in the org policy, it's required
        tag_value = config.get("tag_value", {})
        if "@@assign" in tag_value:
            entry["allowed_values"] = tag_value["@@assign"]
        result[key] = entry
    return result
