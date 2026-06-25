# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Inference Worker Lambda — processes a batch of resources through Tiers 1-3.

Invoked by Step Functions Distributed Map. One invocation per batch (~20 resources).
Runs deterministic tiers only (CFN, CloudTrail, Neighbor). Resources that cannot be
resolved are flagged for Tier 4 (Bedrock Batch) or Tier 5 (Manual).

Input (event from Distributed Map ItemBatcher):
    Items (list): Batch of resource dicts from JSONL
    BatchInput (dict): Contains region from parent state

Output:
    list: Resource dicts with inference results attached
"""

import json
import logging
import boto3
from datetime import datetime, timezone, timedelta
from collections import Counter

from config import (
    load_tag_policy,
    REGION,
    NEIGHBOR_CONSENSUS_THRESHOLD,
    CREATE_EVENT_MAP,
    CLOUDTRAIL_LOOKBACK_DAYS,
    ORPHAN_INACTIVITY_DAYS,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def tier1_stack(resource: dict) -> dict | None:
    """Inherit tags from the CloudFormation stack that manages this resource.

    The Discovery Lambda pre-classifies resources as IaC-managed and
    attaches stack_tags. Tier 1 simply applies those tags if the resource
    is missing them.
    """
    stack_name = resource.get("managed_by")
    stack_tags = resource.get("stack_tags", {})
    if not stack_name or not stack_tags:
        return None

    # Only suggest tags the resource is actually missing
    existing = set(resource.get("tags", {}).keys())
    suggestions = {k: v for k, v in stack_tags.items() if k not in existing}
    if not suggestions:
        return None

    return {
        "suggested_tags": suggestions, "confidence": 99, "tier": 1,
        "method": "CloudFormation stack",
        "evidence": f"Stack: {stack_name}",
    }


def tier2_cloudtrail(resource: dict, trail) -> dict | None:
    """Find who created this resource via CloudTrail."""
    resource_type = resource["resource_type"]
    arn = resource["arn"]
    resource_id = arn.split("/")[-1] if "/" in arn else arn.split(":")[-1]
    event_name = CREATE_EVENT_MAP.get(resource_type)
    if not event_name:
        return None
    try:
        start_time = datetime.now(timezone.utc) - timedelta(days=CLOUDTRAIL_LOOKBACK_DAYS)
        resp = trail.lookup_events(
            LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": event_name}],
            StartTime=start_time, MaxResults=50,
        )
        for event in resp.get("Events", []):
            resource_names = [r.get("ResourceName", "") for r in event.get("Resources", [])]
            if resource_id in resource_names or resource_id in event.get("CloudTrailEvent", ""):
                user = event.get("Username", "unknown")
                created_date = event["EventTime"].strftime("%Y-%m-%d")
                return {
                    "suggested_tags": {"Owner": user, "CreatedBy": user, "CreatedDate": created_date},
                    "confidence": 95, "tier": 2, "method": "CloudTrail creator",
                    "evidence": f"Created by {user} on {created_date} via {event_name}",
                }
    except Exception as e:
        logger.debug("Tier 2 error for %s: %s", resource_id, e)
    return None


def _get_vpc_id(resource: dict, ec2) -> str | None:
    """Determine VPC ID for any VPC-attached resource type."""
    resource_type = resource["resource_type"]
    resource_id = resource["arn"].split("/")[-1] if "/" in resource["arn"] else resource["arn"].split(":")[-1]

    try:
        if resource_type == "ec2:instance":
            resp = ec2.describe_instances(InstanceIds=[resource_id])
            return resp["Reservations"][0]["Instances"][0].get("VpcId")
        elif resource_type == "ec2:security-group":
            resp = ec2.describe_security_groups(GroupIds=[resource_id])
            return resp["SecurityGroups"][0].get("VpcId")
        elif resource_type == "ec2:subnet":
            resp = ec2.describe_subnets(SubnetIds=[resource_id])
            return resp["Subnets"][0].get("VpcId")
        elif resource_type == "ec2:network-interface":
            resp = ec2.describe_network_interfaces(NetworkInterfaceIds=[resource_id])
            return resp["NetworkInterfaces"][0].get("VpcId")
        elif resource_type == "ec2:volume":
            resp = ec2.describe_volumes(VolumeIds=[resource_id])
            attachments = resp["Volumes"][0].get("Attachments", [])
            if attachments:
                inst_resp = ec2.describe_instances(InstanceIds=[attachments[0]["InstanceId"]])
                return inst_resp["Reservations"][0]["Instances"][0].get("VpcId")
        elif resource_type.startswith("elasticloadbalancing:"):
            elbv2 = boto3.client("elbv2", region_name=ec2.meta.region_name)
            resp = elbv2.describe_load_balancers(Names=[resource_id])
            return resp["LoadBalancers"][0].get("VpcId")
        elif resource_type == "rds:db":
            rds = boto3.client("rds", region_name=ec2.meta.region_name)
            resp = rds.describe_db_instances(DBInstanceIdentifier=resource_id)
            return resp["DBInstances"][0].get("DBSubnetGroup", {}).get("VpcId")
        elif resource_type == "lambda:function":
            lam = boto3.client("lambda", region_name=ec2.meta.region_name)
            resp = lam.get_function_configuration(FunctionName=resource_id)
            vpc_config = resp.get("VpcConfig", {})
            return vpc_config.get("VpcId") if vpc_config.get("SubnetIds") else None
    except Exception:
        pass
    return None


def tier3_neighbor(resource: dict, ec2, tag_policy: dict) -> dict | None:
    """Infer tags from well-tagged neighbors in the same VPC.

    Supports all VPC-attached resource types. Finds the resource's VPC,
    then uses tagged EC2 instances in that VPC as the consensus peer pool.
    Skips default VPCs.
    """
    resource_id = resource["arn"].split("/")[-1] if "/" in resource["arn"] else resource["arn"].split(":")[-1]

    vpc_id = _get_vpc_id(resource, ec2)
    if not vpc_id:
        return None

    required_keys = [k for k, v in tag_policy.items() if v.get("required")]
    try:
        # Collect tagged peers from multiple resource types in the same VPC
        peer_tags = []

        # EC2 instances
        instances = ec2.describe_instances(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        for res in instances.get("Reservations", []):
            for inst in res.get("Instances", []):
                if inst["InstanceId"] == resource_id:
                    continue
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", []) if not t["Key"].startswith("aws:")}
                if any(k in tags for k in required_keys):
                    peer_tags.append(tags)

        # Security groups in same VPC
        try:
            sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
            for sg in sgs.get("SecurityGroups", []):
                if sg["GroupId"] == resource_id:
                    continue
                tags = {t["Key"]: t["Value"] for t in sg.get("Tags", []) if not t["Key"].startswith("aws:")}
                if any(k in tags for k in required_keys):
                    peer_tags.append(tags)
        except Exception:
            pass

        # Network interfaces in same VPC
        try:
            enis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
            for eni in enis.get("NetworkInterfaces", []):
                if eni["NetworkInterfaceId"] == resource_id:
                    continue
                tags = {t["Key"]: t["Value"] for t in eni.get("TagSet", []) if not t["Key"].startswith("aws:")}
                if any(k in tags for k in required_keys):
                    peer_tags.append(tags)
        except Exception:
            pass

        if len(peer_tags) < 3:
            return None
        consensus = {}
        for key in required_keys:
            vals = [t[key] for t in peer_tags if key in t]
            if not vals:
                continue
            top, count = Counter(vals).most_common(1)[0]
            if count / len(peer_tags) >= NEIGHBOR_CONSENSUS_THRESHOLD:
                consensus[key] = top
        if consensus:
            return {
                "suggested_tags": consensus,
                "confidence": int(len(consensus) / len(required_keys) * 80),
                "tier": 3, "method": "Neighbor consensus",
                "evidence": f"{len(peer_tags)} peers in VPC {vpc_id}",
            }
    except Exception as e:
        logger.debug("Tier 3 error for %s: %s", resource_id, e)
    return None


def check_orphan(resource: dict, cw) -> bool:
    """Check CloudWatch metrics for signs of an unused resource."""
    resource_type = resource["resource_type"]
    resource_id = resource["arn"].split("/")[-1] if "/" in resource["arn"] else resource["arn"].split(":")[-1]
    metric_checks = {
        "ec2:instance": ("AWS/EC2", "CPUUtilization", "InstanceId"),
        "lambda:function": ("AWS/Lambda", "Invocations", "FunctionName"),
        "rds:db": ("AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier"),
    }
    if resource_type not in metric_checks:
        return False
    namespace, metric_name, dim_name = metric_checks[resource_type]
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=ORPHAN_INACTIVITY_DAYS)
        resp = cw.get_metric_data(
            MetricDataQueries=[{"Id": "m1", "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric_name,
                           "Dimensions": [{"Name": dim_name, "Value": resource_id}]},
                "Period": 86400, "Stat": "Sum"}}],
            StartTime=start, EndTime=end,
        )
        vals = resp["MetricDataResults"][0].get("Values", [])
        return all(v == 0 for v in vals) if vals else True
    except Exception:
        return False


def process_resource(resource: dict, tag_policy: dict, clients: dict) -> dict:
    """Run Tiers 1-3 on a single resource. Flag for Tier 4/5 if unresolved."""
    if not resource.get("missing_tags"):
        return {**resource, "inference": {"tier": 0, "method": "Already compliant",
                                          "suggested_tags": {}, "confidence": 100}}

    missing = resource["missing_tags"]
    start_tier = int(os.environ.get("START_FROM_TIER", "1"))

    # Tier 1
    if start_tier <= 1:
        r = tier1_stack(resource)
        if r and any(k in r.get("suggested_tags", {}) for k in missing):
            return {**resource, "inference": r}

    # Tier 2
    if start_tier <= 2:
        r = tier2_cloudtrail(resource, clients["trail"])
        if r:
            return {**resource, "inference": r}

    # Tier 3
    if start_tier <= 3:
        r = tier3_neighbor(resource, clients["ec2"], tag_policy)
        if r and r["confidence"] >= 60:
            return {**resource, "inference": r}

    # Check orphan status
    is_orphan = check_orphan(resource, clients["cw"])

    # Determine if Tier 4 (Bedrock) should be attempted based on signal quality
    existing_tags = resource.get("tags", {})
    has_signal = (
        "Name" in existing_tags or "creator" in existing_tags or
        "creatorUserId" in existing_tags or "lambda:createdBy" in existing_tags or
        len(existing_tags) >= 2
    )

    return {
        **resource,
        "inference": {
            "tier": 4 if has_signal else 5,
            "method": "Pending Bedrock Batch" if has_signal else "Manual review",
            "suggested_tags": {},
            "confidence": 0,
            "evidence": "Queued for AI inference" if has_signal else "No signal from Tiers 1-3",
            "is_likely_orphan": is_orphan,
            "orphan_note": f"No usage in {ORPHAN_INACTIVITY_DAYS} days — consider terminating" if is_orphan else "",
        },
    }


def handler(event, context):
    """Distributed Map worker — processes a batch of JSONL items."""
    region = event.get("BatchInput", {}).get("region", REGION)
    tag_policy = load_tag_policy()

    logger.info("Worker invoked: %d items", len(event.get("Items", [])))

    clients = {
        "cfn": boto3.client("cloudformation", region_name=region),
        "trail": boto3.client("cloudtrail", region_name=region),
        "ec2": boto3.client("ec2", region_name=region),
        "cw": boto3.client("cloudwatch", region_name=region),
    }

    # Distributed Map ItemBatcher passes items in event["Items"]
    items = event.get("Items", [])
    if isinstance(items, str):
        items = [json.loads(line) for line in items.strip().split("\n") if line.strip()]

    results = []
    for item in items:
        if isinstance(item, str):
            item = json.loads(item)
        results.append(process_resource(item, tag_policy, clients))

    return results
