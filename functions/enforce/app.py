# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Enforce Lambda — generates preventive governance artifacts.

Produces three enforcement artifacts from the tag policy:
    1. AWS Organizations Tag Policy document
    2. Service Control Policy (SCP) denying untagged resource creation
    3. EventBridge rule template for auto-tagging on creation events

These artifacts are saved to S3 and can be deployed to enforce tagging compliance
proactively (vs. TagSense's retroactive remediation).

Input (event):
    run_id (str): Pipeline run identifier
    region (str): AWS region

Output:
    run_id, S3 key of enforcement artifacts, list of artifact types generated
"""

import json
import logging
import boto3
from datetime import datetime, timezone

from config import load_tag_policy, RESULTS_BUCKET, RESULTS_PREFIX, REGION

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _generate_tag_policy(tag_policy: dict) -> dict:
    """Generate an AWS Organizations Tag Policy document.

    See: https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_tag-policies.html
    """
    policy = {"tags": {}}
    for key, config in tag_policy.items():
        entry = {"tag_key": {"@@assign": key}}
        if "allowed_values" in config:
            entry["tag_value"] = {"@@assign": config["allowed_values"]}
        policy["tags"][key] = entry
    return policy


def _generate_scp(tag_policy: dict) -> dict:
    """Generate a Service Control Policy that denies resource creation without required tags.

    WARNING: Test in a sandbox OU before applying to production.
    """
    required_keys = [k for k, v in tag_policy.items() if v.get("required")]
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyUntaggedResources",
            "Effect": "Deny",
            "Action": [
                "ec2:RunInstances",
                "rds:CreateDBInstance",
                "s3:CreateBucket",
                "lambda:CreateFunction",
                "dynamodb:CreateTable",
            ],
            "Resource": "*",
            "Condition": {
                "Null": {f"aws:RequestTag/{k}": "true" for k in required_keys}
            },
        }],
    }


def _generate_eventbridge_rule() -> dict:
    """Generate an EventBridge rule template for auto-tagging on resource creation.

    Captures creation events and routes to a Lambda that applies Owner tags
    from the CloudTrail event's userIdentity.
    """
    return {
        "Description": "TagSense auto-tag on resource creation",
        "EventPattern": {
            "source": ["aws.ec2", "aws.rds", "aws.s3", "aws.lambda"],
            "detail-type": ["AWS API Call via CloudTrail"],
            "detail": {
                "eventName": [
                    "RunInstances",
                    "CreateDBInstance",
                    "CreateBucket",
                    "CreateFunction20150331",
                ]
            },
        },
        "Note": "Attach a Lambda target that reads creator from event and applies Owner tag",
    }


def handler(event, context):
    """Lambda entry point — generates enforcement artifacts."""
    run_id = event["run_id"]
    tag_policy = load_tag_policy()

    logger.info("Generating enforcement artifacts for run_id=%s", run_id)

    s3 = boto3.client("s3")

    output = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "artifacts": {
            "tag_policy": _generate_tag_policy(tag_policy),
            "scp": _generate_scp(tag_policy),
            "eventbridge_rule": _generate_eventbridge_rule(),
        },
        "instructions": {
            "tag_policy": "Deploy via Organizations > Tag policies",
            "scp": "Deploy via Organizations > SCPs — TEST IN SANDBOX OU FIRST",
            "eventbridge_rule": "Deploy via EventBridge + Lambda target",
        },
    }

    key = f"{RESULTS_PREFIX}/{run_id}/enforcement.json"
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=key,
        Body=json.dumps(output, indent=2, default=str),
        ContentType="application/json",
    )

    logger.info("Enforcement artifacts written to s3://%s/%s", RESULTS_BUCKET, key)

    return {
        "run_id": run_id,
        "s3_key": key,
        "artifacts_generated": ["tag_policy", "scp", "eventbridge_rule"],
    }
