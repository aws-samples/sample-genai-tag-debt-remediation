# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Apply Lambda — applies approved tag suggestions to resources.

Reads a review CSV (with human-approved rows), validates scan freshness,
and applies tags via the Resource Groups Tagging API. Dry-run mode by default.

Safety controls:
    - Staleness check: rejects applications from scans older than MAX_SCAN_AGE_HOURS
    - Dry-run default: no tags applied unless explicitly set to dry_run=False
    - aws: prefix filter: never applies AWS-managed tag keys
    - Audit trail: writes a full audit log to S3

Input (event):
    run_id (str): Pipeline run identifier
    dry_run (bool): If True (default), simulate without applying
    region (str): AWS region
    max_scan_age_hours (int): Maximum allowed age of the scan (default: 48)

Output:
    run_id, dry_run status, applied/skipped/error counts, audit S3 key
"""

import json
import csv
import io
import logging
import boto3
from datetime import datetime, timezone, timedelta

from config import RESULTS_BUCKET, RESULTS_PREFIX, REGION

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Maximum age (in hours) before a scan is considered stale and rejected
MAX_SCAN_AGE_HOURS = 48


def _validate_scan_freshness(s3, run_id: str, max_age_hours: int) -> dict | None:
    """Check that the discovery scan is recent enough to apply safely.

    Returns an error dict if stale, or None if fresh.
    """
    try:
        discovery_obj = s3.get_object(
            Bucket=RESULTS_BUCKET,
            Key=f"{RESULTS_PREFIX}/{run_id}/discovery.json",
        )
        discovery = json.loads(discovery_obj["Body"].read())
        scan_timestamp = datetime.fromisoformat(
            discovery["timestamp"].replace("Z", "+00:00")
        )
        age = datetime.now(timezone.utc) - scan_timestamp
        if age > timedelta(hours=max_age_hours):
            return {
                "run_id": run_id,
                "error": "STALE_SCAN",
                "message": (
                    f"Scan is {age.total_seconds() / 3600:.1f} hours old "
                    f"(max: {max_age_hours}h). Re-run discovery for fresh recommendations."
                ),
                "scan_timestamp": scan_timestamp.isoformat(),
            }
    except Exception as e:
        logger.warning("Could not validate scan freshness: %s", e)
    return None


def handler(event, context):
    """Lambda entry point — reads approved CSV and applies tags."""
    run_id = event["run_id"]
    dry_run = event.get("dry_run", True)
    region = event.get("region", REGION)
    max_age_hours = event.get("max_scan_age_hours", MAX_SCAN_AGE_HOURS)

    logger.info(
        "Apply: run_id=%s, dry_run=%s, region=%s", run_id, dry_run, region
    )

    s3 = boto3.client("s3")
    tagging = boto3.client("resourcegroupstaggingapi", region_name=region)

    # Validate scan is not stale
    stale_error = _validate_scan_freshness(s3, run_id, max_age_hours)
    if stale_error:
        logger.error("Stale scan rejected: %s", stale_error["message"])
        return stale_error

    # Read the human-reviewed CSV
    obj = s3.get_object(
        Bucket=RESULTS_BUCKET, Key=f"{RESULTS_PREFIX}/{run_id}/review.csv"
    )
    reader = csv.DictReader(io.StringIO(obj["Body"].read().decode("utf-8")))

    applied, skipped, errors = [], [], []

    for row in reader:
        arn = row.get("ARN", "").strip()
        if not arn:
            continue

        # Only process approved rows
        approval = row.get("Approve (Y/N)", "").strip().upper()
        if approval != "Y":
            skipped.append(arn)
            continue

        # Parse and validate suggested tags
        try:
            tags = json.loads(row.get("Suggested Tags", "{}"))
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in Suggested Tags for %s", arn)
            skipped.append(arn)
            continue

        if not tags or not isinstance(tags, dict):
            skipped.append(arn)
            continue

        # Never apply AWS-managed tag keys
        tags = {k: v for k, v in tags.items() if not k.startswith("aws:")}
        if not tags:
            skipped.append(arn)
            continue

        if dry_run:
            applied.append({"arn": arn, "tags": tags, "action": "DRY_RUN"})
        else:
            try:
                tagging.tag_resources(ResourceARNList=[arn], Tags=tags)
                applied.append({"arn": arn, "tags": tags, "action": "APPLIED"})
            except Exception as e:
                logger.error("Failed to tag %s: %s", arn, e)
                errors.append({"arn": arn, "error": str(e)})

    # Write audit trail
    audit = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "applied": len(applied),
        "skipped": len(skipped),
        "errors": len(errors),
        "details": {"applied": applied, "skipped": skipped, "errors": errors},
    }
    audit_key = f"{RESULTS_PREFIX}/{run_id}/apply_audit.json"
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=audit_key,
        Body=json.dumps(audit, default=str),
        ContentType="application/json",
    )

    logger.info(
        "Apply complete: %d applied, %d skipped, %d errors (dry_run=%s)",
        len(applied), len(skipped), len(errors), dry_run,
    )

    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "applied": len(applied),
        "skipped": len(skipped),
        "errors": len(errors),
        "audit_key": audit_key,
    }
