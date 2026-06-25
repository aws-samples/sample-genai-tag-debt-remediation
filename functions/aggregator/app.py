# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aggregator Lambda — unwraps Distributed Map output and merges results.

The Step Functions Distributed Map writes results in execution-envelope format.
This function reads all output files, unwraps the envelopes, and produces a
single aggregated JSON file for downstream processing.

Input (event):
    run_id (str): Pipeline run identifier
    region (str): AWS region

Output:
    run_id, total processed, resolved count, count needing Bedrock batch
"""

import json
import logging
import boto3

from config import RESULTS_BUCKET, RESULTS_PREFIX, REGION

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _unwrap_map_result(data) -> list:
    """Unwrap Step Functions Distributed Map result envelopes.

    The ResultWriter wraps each batch result in execution metadata:
        [{"Output": <actual_result>, ...}, ...]

    This function handles multiple nesting formats.
    """
    results = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "Output" in item:
                output = item["Output"]
                if isinstance(output, str):
                    try:
                        output = json.loads(output)
                    except json.JSONDecodeError:
                        continue
                if isinstance(output, list):
                    results.extend(output)
                elif isinstance(output, dict):
                    results.append(output)
            elif isinstance(item, dict) and "arn" in item:
                # Already unwrapped resource result
                results.append(item)
    elif isinstance(data, dict) and "arn" in data:
        results.append(data)
    return results


def handler(event, context):
    """Lambda entry point — aggregates Distributed Map output."""
    run_id = event["run_id"]
    region = event.get("region", REGION)
    s3 = boto3.client("s3")

    output_prefix = f"{RESULTS_PREFIX}/{run_id}/map_output/"
    logger.info("Aggregating results from s3://%s/%s", RESULTS_BUCKET, output_prefix)

    all_results = []
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=RESULTS_BUCKET, Prefix=output_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Skip manifest files — only process result JSONs
            if not key.endswith(".json") or "manifest" in key:
                continue
            try:
                body = s3.get_object(Bucket=RESULTS_BUCKET, Key=key)["Body"].read()
                data = json.loads(body)
                results = _unwrap_map_result(data)
                all_results.extend(results)
            except json.JSONDecodeError as e:
                logger.error("Invalid JSON in %s: %s", key, e)
            except Exception as e:
                logger.error("Error processing %s: %s", key, e)

    # Persist aggregated results
    results_key = f"{RESULTS_PREFIX}/{run_id}/tier123_results.json"
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=results_key,
        Body=json.dumps(all_results, default=str),
        ContentType="application/json",
    )

    # Count resources that still need Bedrock batch inference
    needs_bedrock = sum(
        1 for r in all_results
        if r.get("inference", {}).get("tier") == 4
    )
    resolved = len(all_results) - needs_bedrock

    logger.info(
        "Aggregated %d results: %d resolved, %d need Bedrock batch",
        len(all_results), resolved, needs_bedrock,
    )

    return {
        "run_id": run_id,
        "region": region,
        "total_processed": len(all_results),
        "resolved_tiers_123": resolved,
        "needs_bedrock": needs_bedrock,
        "results_key": results_key,
    }
