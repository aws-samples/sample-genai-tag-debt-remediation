# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Discovery Lambda — scans account resources and evaluates tag compliance.

This function:
1. Enumerates all taggable resources via Resource Groups Tagging API
2. Evaluates each resource against the configured tag policy
3. Computes compliance score by resource type
4. Writes non-compliant resources as JSONL for downstream Distributed Map processing

Input (event):
    region (str): AWS region to scan (default: from env)
    run_id (str): Unique pipeline run identifier (auto-generated if omitted)

Output:
    run_id, region, summary stats, S3 location of non-compliant items
"""

import json
import logging
import boto3
from datetime import datetime, timezone

from config import (
    load_tag_policy,
    REGION,
    RESULTS_BUCKET,
    RESULTS_PREFIX,
    CONFIG_RULE_NAME,
    CONFIG_AGGREGATOR_NAME,
    get_noncompliant_resources,
    resolve_arns,
    _config_rule_name,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _parse_resource_type(arn: str) -> str:
    """Extract a normalized service:type string from an ARN.

    Examples:
        arn:aws:ec2:us-east-1:123:instance/i-abc -> ec2:instance
        arn:aws:s3:::my-bucket -> s3:bucket
    """
    parts = arn.split(":")
    if len(parts) < 6:
        return "unknown:unknown"
    service = parts[2]
    resource_part = parts[5] if len(parts) > 5 else "unknown"
    resource_type = resource_part.split("/")[0] if "/" in resource_part else resource_part
    return f"{service}:{resource_type}"


def _evaluate_resource(arn: str, tags: dict, tag_policy: dict, required_keys: list) -> dict:
    """Evaluate a single resource against the tag policy.

    Returns a resource dict with compliance status, missing tags, and invalid values.
    """
    missing = [k for k in required_keys if k not in tags]
    invalid = {}
    for key, policy in tag_policy.items():
        if key in tags and "allowed_values" in policy:
            if tags[key] not in policy["allowed_values"]:
                invalid[key] = {
                    "current": tags[key],
                    "allowed": policy["allowed_values"],
                }

    return {
        "arn": arn,
        "resource_type": _parse_resource_type(arn),
        "tags": tags,
        "missing_tags": missing,
        "invalid_tags": invalid,
        "compliant": len(missing) == 0 and len(invalid) == 0,
    }


def _build_stack_map(cfn) -> dict:
    """Build map of physical resource ID → stack metadata for IaC classification.

    Returns: {physical_id: {"stack_name": str, "tags": dict}}
    Maps both physical ID and ARN forms where available.
    """
    stack_map = {}
    try:
        paginator = cfn.get_paginator("list_stacks")
        for page in paginator.paginate(StackStatusFilter=[
            "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
        ]):
            for summary in page.get("StackSummaries", []):
                stack_name = summary["StackName"]
                try:
                    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
                    tags = {
                        t["Key"]: t["Value"]
                        for t in stack.get("Tags", [])
                        if not t["Key"].startswith("aws:")
                    }
                    resp = cfn.describe_stack_resources(StackName=stack_name)
                    for sr in resp.get("StackResources", []):
                        phys_id = sr.get("PhysicalResourceId", "")
                        if phys_id:
                            stack_map[phys_id] = {"stack_name": stack_name, "tags": tags}
                except Exception:
                    continue
    except Exception as e:
        logger.warning("Failed to build stack map: %s", e)
    logger.info("Stack map built: %d resources across stacks", len(stack_map))
    return stack_map


def _scan_all_resources(tagging, stack_map: dict) -> list:
    """Full-scan mode (default): enumerate every taggable resource in the account.

    Returns a list of raw resource entries {arn, tags, account_id, region,
    [managed_by, stack_tags]}.
    """
    resources_raw = []
    paginator = tagging.get_paginator("get_resources")
    for page in paginator.paginate():
        for resource in page.get("ResourceTagMappingList", []):
            arn = resource["ResourceARN"]
            tags = {
                t["Key"]: t["Value"]
                for t in resource.get("Tags", [])
                if not t["Key"].startswith("aws:")
            }
            resource_id = arn.split("/")[-1] if "/" in arn else arn.split(":")[-1]
            stack_info = stack_map.get(arn) or stack_map.get(resource_id)
            entry = {"arn": arn, "tags": tags}
            if stack_info:
                entry["managed_by"] = stack_info["stack_name"]
                entry["stack_tags"] = stack_info["tags"]
            resources_raw.append(entry)
    return resources_raw


def _scan_from_config(region: str, stack_map: dict, own_account: str) -> list:
    """Config-aware mode: scope discovery to the rule's NON_COMPLIANT resources.

    Uses the Config rule (single-account) or Config aggregator (multi-account)
    to enumerate only non-compliant resources, resolves each to a taggable ARN,
    and returns raw entries carrying account_id + region. Existing tags are not
    fetched from Config here — downstream tiers/report read tags per-resource;
    entries start with empty tags and rely on missing_tags from the policy eval.
    """
    noncompliant = get_noncompliant_resources(
        CONFIG_RULE_NAME, region, CONFIG_AGGREGATOR_NAME or None
    )
    noncompliant = resolve_arns(
        noncompliant, region, CONFIG_AGGREGATOR_NAME or None
    )
    resources_raw = []
    for item in noncompliant:
        arn = item.get("arn", "")
        if not arn:
            continue
        acct = item.get("account_id", own_account)
        entry = {
            "arn": arn,
            "tags": {},
            "account_id": acct,
            "region": item.get("region", region),
            "config_resource_type": item.get("resource_type", ""),
            # A resource flagged NON_COMPLIANT in a different account can't be
            # enriched (Tier 2/3) or tagged by this single-account deployment.
            "cross_account": bool(own_account) and acct != own_account,
        }
        resource_id = arn.split("/")[-1] if "/" in arn else arn.split(":")[-1]
        stack_info = stack_map.get(arn) or stack_map.get(resource_id)
        if stack_info:
            entry["managed_by"] = stack_info["stack_name"]
            entry["stack_tags"] = stack_info["tags"]
        resources_raw.append(entry)
    return resources_raw


def handler(event, context):
    """Lambda entry point for resource discovery and compliance scoring."""
    region = event.get("region", REGION)
    run_id = event.get("run_id", datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    tag_policy = load_tag_policy()
    required_keys = [k for k, v in tag_policy.items() if v.get("required")]

    # Determine discovery mode from Config parameters.
    if CONFIG_RULE_NAME and CONFIG_AGGREGATOR_NAME:
        mode = "config-aggregator"
    elif CONFIG_RULE_NAME:
        mode = "config-rule"
    else:
        mode = "full-scan"

    logger.info(
        "Starting discovery: run_id=%s, region=%s, mode=%s", run_id, region, mode
    )

    tagging = boto3.client("resourcegroupstaggingapi", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)
    s3 = boto3.client("s3")

    try:
        own_account = boto3.client("sts").get_caller_identity()["Account"]
    except Exception:
        own_account = ""

    # Phase 1: Build IaC classification map (local account only).
    stack_map = _build_stack_map(cfn)

    # Phase 2: Enumerate resources per mode.
    if mode == "full-scan":
        resources_raw = _scan_all_resources(tagging, stack_map)
    else:
        resources_raw = _scan_from_config(region, stack_map, own_account)

    # Default region/account for entries produced by full-scan (local account).
    for r in resources_raw:
        r.setdefault("account_id", own_account)
        r.setdefault("region", region)

    # Auto-discover tag policy only in full-scan mode with the hardcoded default.
    # When a Config rule (or explicit TAG_POLICY / Org policy) is the source of
    # truth, we must NOT override it with frequency heuristics.
    from config import DEFAULT_TAG_POLICY
    policy_is_default = (tag_policy == DEFAULT_TAG_POLICY)
    if mode == "full-scan" and policy_is_default and resources_raw:
        from collections import Counter
        # Prefixes that indicate service-managed tags (not governance tags)
        SYSTEM_TAG_PREFIXES = (
            "Explorer", "AmazonDataZone", "lambda:", "serverlessrepo:",
            "elasticmapreduce:", "DLM", "awssupport:", "Patch Group",
        )
        tag_freq = Counter()
        for r in resources_raw:
            for k in r["tags"]:
                if k != "Name" and not k.startswith(SYSTEM_TAG_PREFIXES):
                    tag_freq[k] += 1
        total_raw = len(resources_raw)
        if total_raw > 0:
            # Tags on >20% of resources are likely org standards
            discovered = {
                k: {"required": True, "description": f"Auto-discovered (present on {count}/{total_raw} resources)"}
                for k, count in tag_freq.most_common(10)
                if count / total_raw >= 0.2
            }
            if discovered:
                tag_policy = discovered
                required_keys = list(discovered.keys())
                logger.info("Auto-discovered tag policy: %s", list(discovered.keys()))
            else:
                logger.info("No tags found on >20%% of resources; using default policy")

    # Evaluate compliance
    resources = []
    for r in resources_raw:
        evaluated = _evaluate_resource(r["arn"], r["tags"], tag_policy, required_keys)
        # Carry account/region and cross-account flag through to downstream tiers.
        evaluated["account_id"] = r.get("account_id", own_account)
        evaluated["region"] = r.get("region", region)
        if r.get("cross_account"):
            evaluated["cross_account"] = True
        if r.get("config_resource_type"):
            evaluated["config_resource_type"] = r["config_resource_type"]
        if r.get("managed_by"):
            evaluated["managed_by"] = r["managed_by"]
            evaluated["stack_tags"] = r["stack_tags"]
        resources.append(evaluated)

    # In Config-aware mode every returned resource is already NON_COMPLIANT per
    # the rule; treat them as non-compliant even if our local policy eval (with
    # empty tags) would agree anyway. In full-scan mode use our own evaluation.
    total = len(resources)
    compliant_count = sum(1 for r in resources if r["compliant"])
    non_compliant = [r for r in resources if not r["compliant"]]

    # Breakdown by resource type
    by_type = {}
    for r in resources:
        rt = r["resource_type"]
        by_type.setdefault(rt, {"total": 0, "compliant": 0, "non_compliant": 0})
        by_type[rt]["total"] += 1
        by_type[rt]["compliant" if r["compliant"] else "non_compliant"] += 1

    # IaC coverage metrics
    managed_count = sum(1 for r in resources if r.get("managed_by"))
    unmanaged_count = total - managed_count

    # Cross-account visibility (aggregator mode).
    cross_account_count = sum(1 for r in resources if r.get("cross_account"))

    summary = {
        "total_resources": total,
        "compliant": compliant_count,
        "non_compliant": total - compliant_count,
        "compliance_pct": round(compliant_count / total * 100, 1) if total else 0,
        "iac_managed": managed_count,
        "iac_unmanaged": unmanaged_count,
        "iac_coverage_pct": round(managed_count / total * 100, 1) if total else 0,
        "by_resource_type": by_type,
        "discovery_mode": mode,
        "config_rule": _config_rule_name(CONFIG_RULE_NAME) if CONFIG_RULE_NAME else None,
        "config_aggregator": CONFIG_AGGREGATOR_NAME or None,
        "cross_account_resources": cross_account_count,
    }

    logger.info(
        "Discovery complete: mode=%s, %d resources, %d compliant (%.1f%%), %d cross-account",
        mode, total, compliant_count, summary["compliance_pct"], cross_account_count,
    )

    # Write non-compliant resources as JSONL for Step Functions Distributed Map
    items_key = f"{RESULTS_PREFIX}/{run_id}/items.jsonl"
    lines = "\n".join(json.dumps(r, default=str) for r in non_compliant)
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=items_key,
        Body=lines,
        ContentType="application/jsonlines",
    )

    # Write discovery summary for downstream functions
    discovery_output = {
        "run_id": run_id,
        "region": region,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tag_policy": tag_policy,
        "summary": summary,
    }
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=f"{RESULTS_PREFIX}/{run_id}/discovery.json",
        Body=json.dumps(discovery_output, default=str),
        ContentType="application/json",
    )

    return {
        "run_id": run_id,
        "region": region,
        "mode": mode,
        "summary": summary,
        "non_compliant_count": len(non_compliant),
        "items_s3": {"Bucket": RESULTS_BUCKET, "Key": items_key},
    }
