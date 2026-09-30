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

# --- AWS Config integration (optional) ---
# When CONFIG_RULE_NAME is set, TagSense uses the Config rule as the tag-policy
# source of truth and scopes discovery to the resources that rule flagged
# NON_COMPLIANT (instead of scanning every taggable resource in the account).
#
# Accepts either a bare rule name ("required-tags") or a full rule ARN
# ("arn:aws:config:us-east-1:123456789012:config-rule/config-rule-abc123") — the
# ARN is normalized to a name before use, because every Config compliance API is
# keyed on the rule NAME, not the ARN. See _config_rule_name().
CONFIG_RULE_NAME = os.environ.get("CONFIG_RULE_NAME", "")

# When CONFIG_AGGREGATOR_NAME is also set, TagSense queries the Config
# *aggregator* (org-wide / multi-account view) instead of the local account's
# Config, so discovery covers non-compliant resources across every source
# account and region the aggregator collects. NOTE: v1's downstream Tier 2
# (CloudTrail) / Tier 3 (VPC neighbor) inference and the Apply Lambda are
# single-account (they use the deployed Lambda's own execution role). Resources
# discovered in OTHER accounts are carried through with their account_id/region
# for reporting, but cross-account enrichment/remediation requires the optional
# extensions documented in the README ("Extending to cross-account
# enrichment/remediation").
CONFIG_AGGREGATOR_NAME = os.environ.get("CONFIG_AGGREGATOR_NAME", "")

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
    """Load tag policy from environment, Config rule, Organizations API, or default.

    Priority:
        1. TAG_POLICY env var (JSON string) — explicit override
        2. AWS Config rule (if CONFIG_RULE_NAME set) — the rule's required-tags
           InputParameters become the policy (source of truth for Config-aware mode)
        3. AWS Organizations effective tag policy (if in an org with tag policies)
        4. DEFAULT_TAG_POLICY constant

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
            logger.error("Invalid JSON in TAG_POLICY env var, trying next source")

    # Priority 2: AWS Config rule (Config-aware mode)
    if CONFIG_RULE_NAME:
        try:
            parsed = load_tag_policy_from_config_rule(
                CONFIG_RULE_NAME, CONFIG_AGGREGATOR_NAME or None
            )
            if parsed:
                logger.info(
                    "Loaded tag policy from Config rule %s (%d keys)",
                    _config_rule_name(CONFIG_RULE_NAME), len(parsed),
                )
                return parsed
        except Exception as e:
            logger.warning("Config rule tag policy not available: %s", e)

    # Priority 3: Pull from AWS Organizations effective tag policy
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

    # Priority 4: Default
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


# --- AWS Config helpers -------------------------------------------------------
# These make CONFIG_RULE_NAME / CONFIG_AGGREGATOR_NAME functional. Config's
# compliance and rule-describe APIs are all keyed on the rule NAME (pattern
# [A-Za-z0-9_-]+), never the ARN, so we accept either and normalize to a name.

def _config_rule_name(value: str) -> str:
    """Normalize a Config rule identifier to its bare rule name.

    Accepts either a name ("required-tags") or a full ARN
    ("arn:aws:config:us-east-1:123456789012:config-rule/config-rule-abc123")
    and returns the name. For an ARN, the name is the trailing segment after
    "config-rule/". Non-ARN input is returned unchanged (already a name).

    We normalize to a name because every Config API used here
    (get[_aggregate]_compliance_details_by_config_rule, describe_config_rules,
    describe_organization_config_rule) takes the rule NAME, not the ARN.
    """
    if not value:
        return ""
    v = value.strip()
    if v.startswith("arn:"):
        # arn:aws:config:<region>:<account>:config-rule/<name>
        marker = ":config-rule/"
        idx = v.find(marker)
        if idx != -1:
            return v[idx + len(marker):]
        # Unknown ARN shape — fall back to the last path/colon segment.
        return v.split("/")[-1].split(":")[-1]
    return v


def _parse_required_tags_input_parameters(input_parameters: str) -> dict:
    """Parse the InputParameters of the AWS-managed REQUIRED_TAGS rule into policy.

    The managed `required-tags` rule stores its parameters as a JSON string:
        {"tag1Key": "Environment", "tag1Value": "prod,staging,dev",
         "tag2Key": "Owner", ...}   (up to tag6Key/tag6Value)
    tagNValue is an optional comma-separated allowed-values list.

    Returns TagSense policy format:
        {"Environment": {"required": True, "allowed_values": ["prod","staging","dev"]},
         "Owner": {"required": True}}
    """
    if not input_parameters:
        return {}
    try:
        params = json.loads(input_parameters)
    except (json.JSONDecodeError, TypeError):
        return {}

    policy = {}
    for i in range(1, 7):  # required-tags supports tag1..tag6
        key = params.get(f"tag{i}Key")
        if not key:
            continue
        entry = {"required": True}
        raw_values = params.get(f"tag{i}Value")
        if raw_values:
            values = [v.strip() for v in str(raw_values).split(",") if v.strip()]
            if values:
                entry["allowed_values"] = values
        policy[key] = entry
    return policy


def load_tag_policy_from_config_rule(rule_identifier: str, aggregator_name: str = None) -> dict:
    """Build a tag policy from an AWS Config rule's required-tags parameters.

    For an organization-deployed rule queried through an aggregator, the rule
    definition lives centrally, so we read it via DescribeOrganizationConfigRule.
    Otherwise we read the local account's rule via DescribeConfigRules. In both
    cases we parse the REQUIRED_TAGS InputParameters into the internal policy
    format.

    Returns {} when the rule can't be read or carries no tag parameters, letting
    load_tag_policy() fall through to the next source.
    """
    name = _config_rule_name(rule_identifier)
    if not name:
        return {}
    cfg = boto3.client("config")

    # Aggregator mode: the rule is typically an org config rule defined centrally.
    if aggregator_name:
        try:
            resp = cfg.describe_organization_config_rules(
                OrganizationConfigRuleNames=[name]
            )
            rules = resp.get("OrganizationConfigRules", [])
            if rules:
                managed = (
                    rules[0]
                    .get("OrganizationManagedRuleMetadata", {})
                )
                params = managed.get("InputParameters", "")
                parsed = _parse_required_tags_input_parameters(params)
                if parsed:
                    return parsed
        except Exception as e:  # noqa: BLE001
            logger.debug("DescribeOrganizationConfigRules failed for %s: %s", name, e)
        # Fall through to local describe as a best effort.

    try:
        resp = cfg.describe_config_rules(ConfigRuleNames=[name])
        rules = resp.get("ConfigRules", [])
        if rules:
            params = rules[0].get("InputParameters", "")
            return _parse_required_tags_input_parameters(params)
    except Exception as e:  # noqa: BLE001
        logger.debug("DescribeConfigRules failed for %s: %s", name, e)
    return {}


def get_noncompliant_resources(rule_identifier: str, region: str,
                               aggregator_name: str = None) -> list:
    """Return NON_COMPLIANT resources for a Config rule.

    Each item: {"resource_type": "AWS::S3::Bucket", "resource_id": "...",
                "account_id": "...", "region": "..."}.

    Single-account (no aggregator): uses GetComplianceDetailsByConfigRule; the
    account_id/region default to the local account and the passed-in region.

    Aggregator: uses GetAggregateComplianceDetailsByConfigRule per (account,
    region) source pair discovered from DescribeConfigurationAggregators, so the
    result spans every account/region the aggregator collects.
    """
    name = _config_rule_name(rule_identifier)
    if not name:
        return []
    cfg = boto3.client("config", region_name=region)
    out = []

    if aggregator_name:
        for account_id, src_region in _aggregator_sources(cfg, aggregator_name):
            try:
                paginator = cfg.get_paginator(
                    "get_aggregate_compliance_details_by_config_rule"
                )
                for page in paginator.paginate(
                    ConfigurationAggregatorName=aggregator_name,
                    ConfigRuleName=name,
                    AccountId=account_id,
                    AwsRegion=src_region,
                    ComplianceType="NON_COMPLIANT",
                ):
                    for r in page.get("AggregateEvaluationResults", []):
                        qual = r.get("EvaluationResultIdentifier", {}).get(
                            "EvaluationResultQualifier", {}
                        )
                        out.append({
                            "resource_type": qual.get("ResourceType", ""),
                            "resource_id": qual.get("ResourceId", ""),
                            "account_id": r.get("AccountId", account_id),
                            "region": r.get("AwsRegion", src_region),
                        })
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Aggregate compliance query failed for %s/%s: %s",
                    account_id, src_region, e,
                )
        return out

    # Single-account
    local_account = _get_account_id()
    try:
        paginator = cfg.get_paginator("get_compliance_details_by_config_rule")
        for page in paginator.paginate(
            ConfigRuleName=name, ComplianceTypes=["NON_COMPLIANT"]
        ):
            for r in page.get("EvaluationResults", []):
                qual = r.get("EvaluationResultIdentifier", {}).get(
                    "EvaluationResultQualifier", {}
                )
                out.append({
                    "resource_type": qual.get("ResourceType", ""),
                    "resource_id": qual.get("ResourceId", ""),
                    "account_id": local_account,
                    "region": region,
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("Compliance query failed for rule %s: %s", name, e)
    return out


def _aggregator_sources(cfg, aggregator_name: str) -> list:
    """Return [(account_id, region), ...] source pairs for an aggregator.

    Expands both account-based aggregation sources (explicit AccountIds x
    AwsRegions) and, when present, the organization aggregation source. For an
    org source the concrete member-account list isn't enumerated here (that
    needs Organizations access); callers pass AllAwsRegions org sources through
    SelectAggregateResourceConfig instead. We best-effort return explicit pairs.
    """
    pairs = []
    try:
        resp = cfg.describe_configuration_aggregators(
            ConfigurationAggregatorNames=[aggregator_name]
        )
        for agg in resp.get("ConfigurationAggregators", []):
            for src in agg.get("AccountAggregationSources", []):
                accounts = src.get("AccountIds", [])
                regions = src.get("AwsRegions", [])
                for acct in accounts:
                    for reg in regions:
                        pairs.append((acct, reg))
    except Exception as e:  # noqa: BLE001
        logger.warning("DescribeConfigurationAggregators failed for %s: %s",
                       aggregator_name, e)
    return pairs


# ARN templates by AWS::Service::Type. Single source of truth for turning a
# Config (resource_type, resource_id) pair into a taggable ARN. For many types
# Config's resourceId already IS the id used in the ARN (EC2 instance id, S3
# bucket name, DynamoDB table name); a few (ELBv2, RDS) store the full ARN as
# the resourceId, handled by resolve_arns() preferring Config's own arn field.
def build_resource_arn(resource_type: str, resource_id: str, region: str, account: str) -> str:
    """Deterministically construct a taggable ARN from Config resource metadata.

    resource_type is the Config type string (e.g. "AWS::EC2::Instance"). Falls
    back to returning resource_id unchanged for unknown types (callers should
    prefer an authoritative ARN from resolve_arns() when available).
    """
    if resource_id.startswith("arn:"):
        return resource_id
    kind = (resource_type or "").lower()
    if "ec2::instance" in kind:
        return f"arn:aws:ec2:{region}:{account}:instance/{resource_id}"
    if "s3::bucket" in kind:
        return f"arn:aws:s3:::{resource_id}"
    if "lambda::function" in kind:
        return f"arn:aws:lambda:{region}:{account}:function:{resource_id}"
    if "rds::dbinstance" in kind:
        return f"arn:aws:rds:{region}:{account}:db:{resource_id}"
    if "rds::dbcluster" in kind:
        return f"arn:aws:rds:{region}:{account}:cluster:{resource_id}"
    if "dynamodb::table" in kind:
        return f"arn:aws:dynamodb:{region}:{account}:table/{resource_id}"
    if "elasticloadbalancingv2" in kind or "elasticloadbalancing::loadbalancer" in kind:
        return f"arn:aws:elasticloadbalancing:{region}:{account}:loadbalancer/{resource_id}"
    if "sns::topic" in kind:
        return f"arn:aws:sns:{region}:{account}:{resource_id}"
    if "sqs::queue" in kind:
        return f"arn:aws:sqs:{region}:{account}:{resource_id}"
    if "ecs::cluster" in kind:
        return f"arn:aws:ecs:{region}:{account}:cluster/{resource_id}"
    if "cloudfront::distribution" in kind:
        return f"arn:aws:cloudfront::{account}:distribution/{resource_id}"
    if "kinesis::stream" in kind:
        return f"arn:aws:kinesis:{region}:{account}:stream/{resource_id}"
    return resource_id


def resolve_arns(noncompliant: list, region: str, aggregator_name: str = None) -> list:
    """Attach a taggable 'arn' to each non-compliant resource dict.

    Prefers Config's authoritative ARN:
      - aggregator: SelectAggregateResourceConfig (spans accounts/regions)
      - single-account: BatchGetResourceConfig
    Falls back to build_resource_arn() when Config doesn't return an ARN.

    Input dicts are {resource_type, resource_id, account_id, region}; returns the
    same dicts with an added 'arn' key.
    """
    if not noncompliant:
        return noncompliant
    cfg = boto3.client("config", region_name=region)

    # Build a lookup we can fill from Config's own arn attribute.
    by_key = {
        (r["resource_type"], r["resource_id"]): r for r in noncompliant
    }

    if aggregator_name:
        # One SQL query per resource type keeps the IN-list bounded.
        types = sorted({r["resource_type"] for r in noncompliant if r["resource_type"]})
        for rtype in types:
            ids = [r["resource_id"] for r in noncompliant if r["resource_type"] == rtype]
            for chunk_start in range(0, len(ids), 50):
                chunk = ids[chunk_start:chunk_start + 50]
                id_list = ", ".join("'" + i.replace("'", "") + "'" for i in chunk)
                expr = (
                    "SELECT resourceId, resourceType, arn, accountId, awsRegion "
                    f"WHERE resourceType = '{rtype}' AND resourceId IN ({id_list})"
                )
                try:
                    paginator = cfg.get_paginator("select_aggregate_resource_config")
                    for page in paginator.paginate(
                        Expression=expr,
                        ConfigurationAggregatorName=aggregator_name,
                    ):
                        for row in page.get("Results", []):
                            item = json.loads(row) if isinstance(row, str) else row
                            key = (item.get("resourceType"), item.get("resourceId"))
                            if key in by_key and item.get("arn"):
                                by_key[key]["arn"] = item["arn"]
                except Exception as e:  # noqa: BLE001
                    logger.debug("SelectAggregateResourceConfig failed for %s: %s", rtype, e)
    else:
        keys = [
            {"resourceType": r["resource_type"], "resourceId": r["resource_id"]}
            for r in noncompliant if r["resource_type"] and r["resource_id"]
        ]
        for chunk_start in range(0, len(keys), 100):  # BatchGetResourceConfig max 100
            chunk = keys[chunk_start:chunk_start + 100]
            try:
                resp = cfg.batch_get_resource_config(resourceKeys=chunk)
                for item in resp.get("baseConfigurationItems", []):
                    key = (item.get("resourceType"), item.get("resourceId"))
                    if key in by_key and item.get("arn"):
                        by_key[key]["arn"] = item["arn"]
            except Exception as e:  # noqa: BLE001
                logger.debug("BatchGetResourceConfig failed: %s", e)

    # Fill any gaps deterministically.
    for r in noncompliant:
        if not r.get("arn"):
            r["arn"] = build_resource_arn(
                r["resource_type"], r["resource_id"],
                r.get("region", region), r.get("account_id", ""),
            )
    return noncompliant
