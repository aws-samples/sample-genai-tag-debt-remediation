"""Bedrock Batch Lambda — submits unresolved resources to Bedrock Batch Inference."""

import json
import boto3
from datetime import datetime, timezone
from config import load_tag_policy, REGION, RESULTS_BUCKET, RESULTS_PREFIX, BEDROCK_MODEL_ID, BEDROCK_BATCH_MODEL_ID, BEDROCK_CONFIDENCE_THRESHOLD


def _load_org_context(s3_client):
    """Load org-context.json from S3 if available."""
    try:
        obj = s3_client.get_object(
            Bucket=RESULTS_BUCKET,
            Key=f"{RESULTS_PREFIX}/context/org-context.json"
        )
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def build_prompt(resource, tag_policy, org_context=None):
    existing = resource.get("tags", {})
    missing = resource.get("missing_tags", [])

    # Build org-context steering section
    steering = ""
    if org_context:
        if org_context.get("environments"):
            steering += f"\nValid Environment values: {json.dumps(org_context['environments'])}"
        if org_context.get("cost_centers"):
            steering += f"\nValid CostCenter values: {json.dumps(list(org_context['cost_centers'].keys()))}"
        if org_context.get("applications"):
            steering += f"\nValid Application values: {json.dumps(org_context['applications'])}"
        if org_context.get("teams"):
            steering += f"\nValid Owner/Team values: {json.dumps(list(org_context['teams'].keys()))}"
        if org_context.get("naming_conventions"):
            steering += f"\nNaming conventions: {json.dumps(org_context['naming_conventions'])}"
        if steering:
            steering = f"\n\nOrganization context (CONSTRAIN your suggestions to these valid values when possible):{steering}"

    return f"""You are an AWS resource tagging assistant. Suggest tags for this resource.

Resource: ARN={resource['arn']}, Type={resource['resource_type']}
Existing tags: {json.dumps(existing)}

Required tags (suggest values for THESE missing ones only): {json.dumps({k:v for k,v in tag_policy.items() if k in missing})}
{steering}
Rules:
- Use existing tags, resource name, and ARN to infer required tags.
- For Owner: look at creator, creatorUserId, lambda:createdBy tags.
- For Environment: look at Name patterns (prod, dev, staging).
- For Application: look at Name, AmazonDataZoneProject, stack name patterns.
- For CostCenter: only suggest if clear evidence. Otherwise say "unknown".
- If org context provides valid values, ONLY suggest from those lists.
- State confidence: high/medium/low per tag.
- If you cannot determine a value, say "unknown".
- Respond ONLY with JSON: {{"tags": {{"Key": {{"value":"...","confidence":"high|medium|low","reasoning":"..."}}}}}}"""


def _parse_bedrock_response(text, resource, tag_policy):
    """Parse Bedrock response text into inference result dict."""
    import re
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r'^```(?:json)?\s*', '', clean)
        clean = re.sub(r'\s*```$', '', clean)
    match = re.search(r'\{.*\}', clean, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
        tags_data = parsed.get("tags", {})
        conf_map = {"high": 80, "medium": 60, "low": 30}
        suggested, reasons = {}, []
        for k, info in tags_data.items():
            v = info.get("value", "unknown")
            if v and v != "unknown":
                suggested[k] = v
                reasons.append(f"{k}={v} ({info.get('confidence','low')})")
        if suggested:
            avg = sum(conf_map.get(tags_data[k].get("confidence", "low"), 30) for k in suggested) // len(suggested)
            return {"suggested_tags": suggested, "confidence": avg, "tier": 4,
                    "method": "Bedrock AI (real-time)", "evidence": "; ".join(reasons)}
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def handler(event, context):
    """Reads Tier 1-3 results, submits unresolved resources to Bedrock Batch."""
    run_id = event["run_id"]
    region = event.get("region", REGION)
    tag_policy = load_tag_policy()
    use_thinking = event.get("use_thinking", False)

    s3 = boto3.client("s3")
    bedrock = boto3.client("bedrock", region_name=region)

    # Load org-context for value steering
    org_context = _load_org_context(s3)

    # Determine input source — standard run or thinking retry
    if use_thinking:
        # Thinking retry: read low-confidence resources from previous run
        retry_key = f"{RESULTS_PREFIX}/{run_id}/thinking_retry_input.json"
        obj = s3.get_object(Bucket=RESULTS_BUCKET, Key=retry_key)
        needs_bedrock = json.loads(obj["Body"].read())
        resolved = []  # resolved already written in first pass
    else:
        # Standard first pass
        results_key = f"{RESULTS_PREFIX}/{run_id}/tier123_results.json"
        obj = s3.get_object(Bucket=RESULTS_BUCKET, Key=results_key)
        all_results = json.loads(obj["Body"].read())
        resolved = [r for r in all_results if r.get("inference", {}).get("tier") not in (4,)]
        needs_bedrock = [r for r in all_results if r.get("inference", {}).get("tier") == 4]

    if not needs_bedrock:
        final_key = f"{RESULTS_PREFIX}/{run_id}/inference.json"
        output = _build_output(run_id, region, all_results if not use_thinking else [])
        s3.put_object(Bucket=RESULTS_BUCKET, Key=final_key,
                      Body=json.dumps(output, default=str), ContentType="application/json")
        return {"run_id": run_id, "s3_key": final_key, "batch_job_id": None,
                "bedrock_count": 0, "use_thinking": use_thinking, **_tier_summary(all_results if not use_thinking else [])}

    # Build JSONL input for Bedrock Batch
    batch_lines = []
    for i, resource in enumerate(needs_bedrock):
        model_input = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 500,
            "messages": [{"role": "user", "content": build_prompt(resource, tag_policy, org_context)}]
        }
        # Enable extended thinking for retry pass
        if use_thinking:
            model_input["thinking"] = {"type": "enabled", "budget_tokens": 2000}

        record = {"recordId": str(i), "modelInput": model_input}
        batch_lines.append(json.dumps(record))

    suffix = "_thinking" if use_thinking else ""
    input_key = f"{RESULTS_PREFIX}/{run_id}/bedrock_batch_input{suffix}.jsonl"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=input_key,
                  Body="\n".join(batch_lines), ContentType="application/jsonlines")

    # Bedrock Batch requires minimum 100 records.
    # Batch supports cross-region inference profiles (us. prefix) for newer Claude models.
    # Falls back to real-time if batch is not configured or record count too low.
    MIN_BATCH_RECORDS = 100
    use_batch = (
        len(batch_lines) >= MIN_BATCH_RECORDS
        and BEDROCK_BATCH_MODEL_ID
    )

    if not use_batch:
        reason = "< 100 records" if len(batch_lines) < MIN_BATCH_RECORDS else "no batch-compatible model configured"
        print(f"Using real-time InvokeModel ({reason}, {len(needs_bedrock)} resources, 10 concurrent)")
        bedrock_rt = boto3.client("bedrock-runtime", region_name=region)
        from config import BEDROCK_MODEL_ID as RT_MODEL_ID
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _invoke_single(idx_resource):
            idx, resource = idx_resource
            try:
                resp = bedrock_rt.invoke_model(
                    modelId=RT_MODEL_ID,
                    body=json.dumps({
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": build_prompt(resource, tag_policy, org_context)}],
                    })
                )
                text = json.loads(resp["body"].read())["content"][0]["text"]
                parsed = _parse_bedrock_response(text, resource, tag_policy)
                if parsed:
                    resource["inference"] = parsed
            except Exception as e:
                print(f"InvokeModel failed for {resource.get('arn','')[-40:]}: {e}")

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_invoke_single, (i, r)) for i, r in enumerate(needs_bedrock)]
            for f in as_completed(futures):
                f.result()

        all_final = (resolved if not use_thinking else []) + needs_bedrock
        final_key = f"{RESULTS_PREFIX}/{run_id}/inference.json"
        output = _build_output(run_id, region, all_final)
        s3.put_object(Bucket=RESULTS_BUCKET, Key=final_key,
                      Body=json.dumps(output, default=str), ContentType="application/json")
        return {"run_id": run_id, "s3_key": final_key, "batch_job_arn": None,
                "bedrock_count": len(needs_bedrock), "mode": "real-time",
                "use_thinking": use_thinking, **_tier_summary(all_final)}

    # Batch path — only reached if model supports batch AND enough records
    output_prefix = f"s3://{RESULTS_BUCKET}/{RESULTS_PREFIX}/{run_id}/bedrock_batch_output{suffix}/"

    # Submit batch job — sanitize job name to match API regex constraint
    # Falls back to real-time InvokeModel if batch submission fails
    import re as _re
    clean_name = _re.sub(r'[^a-zA-Z0-9\-]', '', f"tagsense-{run_id}{suffix}")[:63]
    try:
        job = bedrock.create_model_invocation_job(
            jobName=clean_name,
            modelId=BEDROCK_BATCH_MODEL_ID,
            roleArn=event.get("bedrock_role_arn", ""),
            inputDataConfig={
                "s3InputDataConfig": {
                    "s3InputFormat": "JSONL",
                    "s3Uri": f"s3://{RESULTS_BUCKET}/{input_key}"
                }
            },
            outputDataConfig={
                "s3OutputDataConfig": {"s3Uri": output_prefix}
            }
        )
    except Exception as batch_error:
        print(f"Batch submission failed: {batch_error}. Falling back to real-time InvokeModel.")
        bedrock_rt = boto3.client("bedrock-runtime", region_name=region)
        from config import BEDROCK_MODEL_ID as RT_MODEL_ID
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _invoke_single(idx_resource):
            idx, resource = idx_resource
            try:
                resp = bedrock_rt.invoke_model(
                    modelId=RT_MODEL_ID,
                    body=json.dumps({
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": build_prompt(resource, tag_policy, org_context)}],
                    })
                )
                text = json.loads(resp["body"].read())["content"][0]["text"]
                parsed = _parse_bedrock_response(text, resource, tag_policy)
                if parsed:
                    resource["inference"] = parsed
            except Exception as e:
                print(f"InvokeModel failed for {resource.get('arn','')[-40:]}: {e}")

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_invoke_single, (i, r)) for i, r in enumerate(needs_bedrock)]
            for f in as_completed(futures):
                f.result()  # propagate exceptions for logging

        all_final = (resolved if not use_thinking else []) + needs_bedrock
        final_key = f"{RESULTS_PREFIX}/{run_id}/inference.json"
        output = _build_output(run_id, region, all_final)
        s3.put_object(Bucket=RESULTS_BUCKET, Key=final_key,
                      Body=json.dumps(output, default=str), ContentType="application/json")
        return {"run_id": run_id, "s3_key": final_key, "batch_job_arn": None,
                "bedrock_count": len(needs_bedrock), "mode": "real-time-fallback",
                "use_thinking": use_thinking, **_tier_summary(all_final)}

    # Save state for the poller
    state = {
        "run_id": run_id, "region": region,
        "batch_job_arn": job["jobArn"],
        "resolved": resolved,
        "needs_bedrock": needs_bedrock,
        "output_prefix": output_prefix,
        "use_thinking": use_thinking,
    }
    state_key = f"{RESULTS_PREFIX}/{run_id}/batch_state{suffix}.json"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=state_key,
                  Body=json.dumps(state, default=str), ContentType="application/json")

    return {
        "run_id": run_id,
        "batch_job_arn": job["jobArn"],
        "bedrock_count": len(needs_bedrock),
        "resolved_count": len(resolved),
        "mode": "batch",
        "use_thinking": use_thinking,
    }


def _build_output(run_id, region, results):
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    orphans = []
    for r in results:
        t = r.get("inference", {}).get("tier", 5)
        tier_counts[t] = tier_counts.get(t, 0) + 1
        if r.get("inference", {}).get("is_likely_orphan"):
            orphans.append(r["arn"])
    return {
        "run_id": run_id, "region": region,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_inferred": len(results),
        "tier_breakdown": tier_counts,
        "orphan_candidates": orphans,
        "recommendations": results,
    }


def _tier_summary(results):
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    orphans = []
    for r in results:
        t = r.get("inference", {}).get("tier", 5)
        tier_counts[t] = tier_counts.get(t, 0) + 1
        if r.get("inference", {}).get("is_likely_orphan"):
            orphans.append(r["arn"])
    return {"tier_breakdown": tier_counts, "orphan_count": len(orphans), "total_inferred": len(results)}
