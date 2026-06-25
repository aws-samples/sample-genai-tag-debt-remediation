"""Bedrock Batch Poller — checks batch job status, merges results, triggers thinking retry for low-confidence."""

import json
import re
import boto3
from datetime import datetime, timezone
from config import REGION, RESULTS_BUCKET, RESULTS_PREFIX, BEDROCK_CONFIDENCE_THRESHOLD


def handler(event, context):
    """Check batch job status. Returns status for Step Functions Wait/Choice loop."""
    run_id = event["run_id"]
    batch_job_arn = event["batch_job_arn"]
    region = event.get("region", REGION)
    use_thinking = event.get("use_thinking", False)

    bedrock = boto3.client("bedrock", region_name=region)
    s3 = boto3.client("s3")

    job = bedrock.get_model_invocation_job(jobIdentifier=batch_job_arn)
    status = job["status"]

    if status not in ("Completed", "Failed", "Stopped"):
        return {"run_id": run_id, "batch_job_arn": batch_job_arn, "status": status,
                "region": region, "use_thinking": use_thinking}

    suffix = "_thinking" if use_thinking else ""
    state_key = f"{RESULTS_PREFIX}/{run_id}/batch_state{suffix}.json"
    state = json.loads(s3.get_object(Bucket=RESULTS_BUCKET, Key=state_key)["Body"].read())

    if status != "Completed":
        all_results = state["resolved"] + state["needs_bedrock"]
        _write_final(run_id, region, all_results, s3)
        return {"run_id": run_id, "status": "Completed", "batch_status": status,
                "s3_key": f"{RESULTS_PREFIX}/{run_id}/inference.json",
                "needs_thinking_retry": False}

    # Parse batch output
    output_prefix = state["output_prefix"].replace(f"s3://{RESULTS_BUCKET}/", "")
    batch_results = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=RESULTS_BUCKET, Prefix=output_prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".jsonl.out"):
                body = s3.get_object(Bucket=RESULTS_BUCKET, Key=obj["Key"])["Body"].read().decode()
                for line in body.strip().split("\n"):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    rid = record.get("recordId", "")
                    output = record.get("modelOutput", {})
                    text = ""
                    if "content" in output:
                        for block in output["content"]:
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                break
                    batch_results[rid] = text

    # Merge results
    conf_map = {"high": 80, "medium": 60, "low": 30}
    needs_bedrock = state["needs_bedrock"]
    low_confidence = []

    for i, resource in enumerate(needs_bedrock):
        text = batch_results.get(str(i), "")
        if not text:
            continue
        try:
            clean = text.strip()
            if clean.startswith("```"):
                clean = re.sub(r'^```(?:json)?\s*', '', clean)
                clean = re.sub(r'\s*```$', '', clean)
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if not m:
                continue
            parsed = json.loads(m.group())
            tags_data = parsed.get("tags", {})
            suggested, reasons = {}, []
            for k, info in tags_data.items():
                v = info.get("value", "unknown")
                if v and v != "unknown":
                    suggested[k] = v
                    reasons.append(f"{k}={v} ({info.get('confidence','low')}: {info.get('reasoning','N/A')})")
            if suggested:
                avg = sum(conf_map.get(tags_data[k].get("confidence", "low"), 30) for k in suggested) // len(suggested)
                method = "Bedrock Batch AI (extended thinking)" if use_thinking else "Bedrock Batch AI"
                resource["inference"] = {
                    "suggested_tags": suggested, "confidence": avg, "tier": 4,
                    "method": method, "evidence": "; ".join(reasons),
                    "is_likely_orphan": resource.get("inference", {}).get("is_likely_orphan", False),
                    "orphan_note": resource.get("inference", {}).get("orphan_note", ""),
                }
                # Track low-confidence for thinking retry (only on first pass)
                if not use_thinking and avg < BEDROCK_CONFIDENCE_THRESHOLD:
                    low_confidence.append(resource)
        except Exception:
            pass

    # Merge all results
    all_results = state["resolved"] + needs_bedrock

    # If first pass and there are low-confidence results, trigger thinking retry
    needs_thinking_retry = False
    if not use_thinking and low_confidence:
        retry_key = f"{RESULTS_PREFIX}/{run_id}/thinking_retry_input.json"
        s3.put_object(Bucket=RESULTS_BUCKET, Key=retry_key,
                      Body=json.dumps(low_confidence, default=str), ContentType="application/json")
        needs_thinking_retry = True

    _write_final(run_id, region, all_results, s3)

    return {"run_id": run_id, "status": "Completed", "batch_status": status,
            "s3_key": f"{RESULTS_PREFIX}/{run_id}/inference.json",
            "needs_thinking_retry": needs_thinking_retry,
            "thinking_retry_count": len(low_confidence) if not use_thinking else 0,
            **_tier_summary(all_results)}


def _write_final(run_id, region, results, s3):
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    orphans = []
    for r in results:
        t = r.get("inference", {}).get("tier", 5)
        tier_counts[t] = tier_counts.get(t, 0) + 1
        if r.get("inference", {}).get("is_likely_orphan"):
            orphans.append(r["arn"])
    output = {
        "run_id": run_id, "region": region,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_inferred": len(results), "tier_breakdown": tier_counts,
        "orphan_candidates": orphans, "recommendations": results,
    }
    s3.put_object(Bucket=RESULTS_BUCKET, Key=f"{RESULTS_PREFIX}/{run_id}/inference.json",
                  Body=json.dumps(output, default=str), ContentType="application/json")


def _tier_summary(results):
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    orphans = []
    for r in results:
        t = r.get("inference", {}).get("tier", 5)
        tier_counts[t] = tier_counts.get(t, 0) + 1
        if r.get("inference", {}).get("is_likely_orphan"):
            orphans.append(r["arn"])
    return {"tier_breakdown": tier_counts, "orphan_count": len(orphans), "total_inferred": len(results)}
