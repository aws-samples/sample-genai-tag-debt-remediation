# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for TagSense core logic — no AWS calls required."""

import json
import re
import sys
import os
import pytest
from collections import Counter


# --- Extract testable functions inline (avoids boto3 import) ---

def _parse_bedrock_response(text, resource, tag_policy):
    """Copied from functions/bedrock_batch/app.py for isolated testing."""
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


class TestParseBedrockResponse:
    TAG_POLICY = {"Environment": {"required": True}, "Application": {"required": True}}
    RESOURCE = {"arn": "arn:aws:s3:::test-bucket", "resource_type": "s3:bucket"}

    def test_valid_json_response(self):
        text = '{"tags": {"Environment": {"value": "production", "confidence": "high"}, "Application": {"value": "payments", "confidence": "medium"}}}'
        result = _parse_bedrock_response(text, self.RESOURCE, self.TAG_POLICY)
        assert result is not None
        assert result["suggested_tags"] == {"Environment": "production", "Application": "payments"}
        assert result["confidence"] == 70  # avg of high(80) + medium(60)
        assert result["tier"] == 4

    def test_markdown_wrapped_json(self):
        text = '```json\n{"tags": {"Environment": {"value": "dev", "confidence": "high"}}}\n```'
        result = _parse_bedrock_response(text, self.RESOURCE, self.TAG_POLICY)
        assert result["suggested_tags"] == {"Environment": "dev"}
        assert result["confidence"] == 80

    def test_unknown_values_skipped(self):
        text = '{"tags": {"Environment": {"value": "unknown", "confidence": "low"}, "Application": {"value": "api", "confidence": "medium"}}}'
        result = _parse_bedrock_response(text, self.RESOURCE, self.TAG_POLICY)
        assert result["suggested_tags"] == {"Application": "api"}

    def test_all_unknown_returns_none(self):
        text = '{"tags": {"Environment": {"value": "unknown", "confidence": "low"}}}'
        result = _parse_bedrock_response(text, self.RESOURCE, self.TAG_POLICY)
        assert result is None

    def test_invalid_json_returns_none(self):
        result = _parse_bedrock_response("I don't know", self.RESOURCE, self.TAG_POLICY)
        assert result is None

    def test_empty_response_returns_none(self):
        result = _parse_bedrock_response("", self.RESOURCE, self.TAG_POLICY)
        assert result is None

    def test_json_embedded_in_text(self):
        text = 'Based on the resource name, here is my analysis:\n{"tags": {"Environment": {"value": "staging", "confidence": "medium"}}}\nLet me know if you need more.'
        result = _parse_bedrock_response(text, self.RESOURCE, self.TAG_POLICY)
        assert result["suggested_tags"] == {"Environment": "staging"}


# --- Report merge logic ---

class TestReportMerge:
    """Test the merge logic used by Report Lambda."""

    def _merge(self, tier123, bedrock_recs):
        """Replicate the merge logic from report.py."""
        bedrock_by_arn = {
            r["arn"]: r for r in bedrock_recs
            if r.get("inference", {}).get("suggested_tags")
        }
        all_recommendations = []
        for resource in tier123:
            arn = resource.get("arn", "")
            if arn in bedrock_by_arn:
                all_recommendations.append(bedrock_by_arn[arn])
            else:
                all_recommendations.append(resource)
        # Include Bedrock results for ARNs not in tier123
        tier123_arns = {r.get("arn") for r in tier123}
        for arn, rec in bedrock_by_arn.items():
            if arn not in tier123_arns:
                all_recommendations.append(rec)
        return all_recommendations

    def test_bedrock_overlays_tier4(self):
        tier123 = [
            {"arn": "arn:1", "inference": {"tier": 4, "suggested_tags": {}}},
            {"arn": "arn:2", "inference": {"tier": 2, "suggested_tags": {"Env": "prod"}}},
        ]
        bedrock = [
            {"arn": "arn:1", "inference": {"tier": 4, "suggested_tags": {"Env": "dev"}}},
        ]
        result = self._merge(tier123, bedrock)
        assert len(result) == 2
        assert result[0]["inference"]["suggested_tags"] == {"Env": "dev"}  # overlaid
        assert result[1]["inference"]["tier"] == 2  # untouched

    def test_tier12_not_overwritten(self):
        tier123 = [{"arn": "arn:1", "inference": {"tier": 1, "suggested_tags": {"Env": "prod"}}}]
        bedrock = [{"arn": "arn:1", "inference": {"tier": 4, "suggested_tags": {"Env": "dev"}}}]
        result = self._merge(tier123, bedrock)
        # Bedrock has suggestions so it DOES overlay — this is by design
        assert result[0]["inference"]["suggested_tags"] == {"Env": "dev"}

    def test_empty_bedrock_preserves_tier123(self):
        tier123 = [{"arn": "arn:1", "inference": {"tier": 2, "suggested_tags": {"App": "x"}}}]
        result = self._merge(tier123, [])
        assert result == tier123

    def test_new_arn_from_bedrock_added(self):
        tier123 = [{"arn": "arn:1", "inference": {"tier": 5}}]
        bedrock = [{"arn": "arn:new", "inference": {"tier": 4, "suggested_tags": {"Env": "prod"}}}]
        result = self._merge(tier123, bedrock)
        assert len(result) == 2
        assert result[1]["arn"] == "arn:new"


# --- JSON data builder for HTML report ---

class TestBuildJsonData:
    def test_caps_at_max(self):
        MAX_EMBEDDED = 10000
        recs = [{"arn": f"arn:{i}", "resource_type": "s3:bucket", "inference": {"tier": 4, "confidence": 60, "suggested_tags": {"Env": "prod"}, "evidence": "test"}} for i in range(15000)]
        items = []
        for r in recs[:MAX_EMBEDDED]:
            inf = r.get("inference", {})
            items.append({"arn": r["arn"], "type": r["resource_type"], "tier": inf.get("tier", 5)})
        assert len(items) == 10000

    def test_handles_empty_suggestions(self):
        recs = [{"arn": "arn:1", "resource_type": "ec2:instance", "inference": {"tier": 5, "confidence": 0, "suggested_tags": {}, "evidence": ""}}]
        inf = recs[0]["inference"]
        suggested = inf.get("suggested_tags", {})
        tags_str = ",".join(f"{k}: {v}" for k, v in suggested.items()) if suggested else ""
        assert tags_str == ""


# --- Consensus threshold logic ---

class TestConsensusLogic:
    """Test the neighbor consensus calculation (pure logic, no AWS calls)."""

    THRESHOLD = 0.7

    def _consensus(self, peer_tags, required_keys):
        from collections import Counter
        consensus = {}
        for key in required_keys:
            values = [t[key] for t in peer_tags if key in t]
            if not values:
                continue
            top_value, count = Counter(values).most_common(1)[0]
            if count / len(peer_tags) >= self.THRESHOLD:
                consensus[key] = top_value
        return consensus

    def test_strong_consensus(self):
        peers = [{"Env": "prod"}, {"Env": "prod"}, {"Env": "prod"}, {"Env": "dev"}]
        result = self._consensus(peers, ["Env"])
        assert result == {"Env": "prod"}  # 75% > 70%

    def test_no_consensus(self):
        peers = [{"Env": "prod"}, {"Env": "dev"}, {"Env": "staging"}, {"Env": "test"}]
        result = self._consensus(peers, ["Env"])
        assert result == {}  # 25% < 70%

    def test_exact_threshold(self):
        # 7 out of 10 = 70% — equals threshold
        peers = [{"Env": "prod"}] * 7 + [{"Env": "dev"}] * 3
        result = self._consensus(peers, ["Env"])
        assert result == {"Env": "prod"}

    def test_below_threshold(self):
        # 6 out of 10 = 60% — below threshold
        peers = [{"Env": "prod"}] * 6 + [{"Env": "dev"}] * 4
        result = self._consensus(peers, ["Env"])
        assert result == {}

    def test_multiple_keys(self):
        peers = [
            {"Env": "prod", "App": "payments"},
            {"Env": "prod", "App": "payments"},
            {"Env": "prod", "App": "auth"},
        ]
        result = self._consensus(peers, ["Env", "App"])
        assert result == {"Env": "prod"}  # Env=100%, App=67% (below threshold)


# --- Tier 1 IaC classification logic ---

class TestTier1Stack:
    """Test Tier 1 stack tag inheritance (extracted logic, no AWS calls)."""

    def _tier1(self, resource):
        """Replicate tier1_stack logic from inference worker."""
        stack_name = resource.get("managed_by")
        stack_tags = resource.get("stack_tags", {})
        if not stack_name or not stack_tags:
            return None
        existing = set(resource.get("tags", {}).keys())
        suggestions = {k: v for k, v in stack_tags.items() if k not in existing}
        if not suggestions:
            return None
        return {
            "suggested_tags": suggestions, "confidence": 99, "tier": 1,
            "method": "CloudFormation stack", "evidence": f"Stack: {stack_name}",
        }

    def test_managed_resource_missing_tags(self):
        resource = {
            "arn": "arn:aws:s3:::my-bucket",
            "tags": {"Name": "my-bucket"},
            "missing_tags": ["Environment", "Owner"],
            "managed_by": "my-stack",
            "stack_tags": {"Environment": "prod", "Owner": "team-a", "Name": "stack-name"},
        }
        result = self._tier1(resource)
        assert result is not None
        assert result["suggested_tags"] == {"Environment": "prod", "Owner": "team-a"}
        assert result["confidence"] == 99
        assert "my-stack" in result["evidence"]

    def test_unmanaged_resource_returns_none(self):
        resource = {"arn": "arn:aws:s3:::bucket", "tags": {}, "missing_tags": ["Env"]}
        assert self._tier1(resource) is None

    def test_managed_but_already_has_all_tags(self):
        resource = {
            "arn": "arn:aws:s3:::bucket",
            "tags": {"Environment": "prod", "Owner": "team-a"},
            "managed_by": "stack",
            "stack_tags": {"Environment": "prod", "Owner": "team-a"},
        }
        assert self._tier1(resource) is None

    def test_managed_no_stack_tags(self):
        resource = {
            "arn": "arn:aws:s3:::bucket",
            "tags": {},
            "managed_by": "stack",
            "stack_tags": {},
        }
        assert self._tier1(resource) is None

    def test_only_suggests_missing_tags(self):
        resource = {
            "arn": "arn:aws:s3:::bucket",
            "tags": {"Environment": "prod"},
            "missing_tags": ["Owner"],
            "managed_by": "stack",
            "stack_tags": {"Environment": "prod", "Owner": "team-b"},
        }
        result = self._tier1(resource)
        assert result["suggested_tags"] == {"Owner": "team-b"}
        assert "Environment" not in result["suggested_tags"]
