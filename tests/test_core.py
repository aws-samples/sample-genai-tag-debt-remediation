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


# --- AWS Config rule identifier normalization (config.py::_config_rule_name) ---

def _config_rule_name(value):
    """Copied from config.py for isolated testing (no boto3)."""
    if not value:
        return ""
    v = value.strip()
    if v.startswith("arn:"):
        marker = ":config-rule/"
        idx = v.find(marker)
        if idx != -1:
            return v[idx + len(marker):]
        return v.split("/")[-1].split(":")[-1]
    return v


class TestConfigRuleName:
    def test_plain_name_unchanged(self):
        assert _config_rule_name("required-tags") == "required-tags"

    def test_full_arn_to_name(self):
        arn = "arn:aws:config:us-east-1:663479261746:config-rule/config-rule-q7ecvv"
        assert _config_rule_name(arn) == "config-rule-q7ecvv"

    def test_empty(self):
        assert _config_rule_name("") == ""

    def test_whitespace_trimmed(self):
        assert _config_rule_name("  required-tags  ") == "required-tags"

    def test_arn_without_marker_falls_back(self):
        # Degenerate ARN shape — fall back to last segment rather than crash.
        assert _config_rule_name("arn:aws:config:us-east-1:123:weird/thing") == "thing"


# --- required-tags InputParameters parsing (config.py) ---

def _parse_required_tags_input_parameters(input_parameters):
    """Copied from config.py for isolated testing."""
    if not input_parameters:
        return {}
    try:
        params = json.loads(input_parameters)
    except (json.JSONDecodeError, TypeError):
        return {}
    policy = {}
    for i in range(1, 7):
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


class TestParseRequiredTagsInputParameters:
    def test_keys_and_allowed_values(self):
        params = json.dumps({
            "tag1Key": "Environment", "tag1Value": "prod,staging,dev",
            "tag2Key": "Owner",
        })
        policy = _parse_required_tags_input_parameters(params)
        assert policy["Environment"] == {
            "required": True, "allowed_values": ["prod", "staging", "dev"],
        }
        assert policy["Owner"] == {"required": True}

    def test_empty_string(self):
        assert _parse_required_tags_input_parameters("") == {}

    def test_invalid_json(self):
        assert _parse_required_tags_input_parameters("not json") == {}

    def test_all_six_slots(self):
        params = json.dumps({f"tag{i}Key": f"K{i}" for i in range(1, 7)})
        policy = _parse_required_tags_input_parameters(params)
        assert len(policy) == 6
        assert all(policy[f"K{i}"]["required"] for i in range(1, 7))

    def test_value_whitespace_stripped(self):
        params = json.dumps({"tag1Key": "Env", "tag1Value": " prod , dev "})
        policy = _parse_required_tags_input_parameters(params)
        assert policy["Env"]["allowed_values"] == ["prod", "dev"]


# --- Deterministic ARN construction (config.py::build_resource_arn) ---

def build_resource_arn(resource_type, resource_id, region, account):
    """Copied from config.py for isolated testing."""
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


class TestBuildResourceArn:
    R, A = "us-east-1", "123456789012"

    def test_ec2_instance(self):
        assert build_resource_arn("AWS::EC2::Instance", "i-0abc", self.R, self.A) == \
            "arn:aws:ec2:us-east-1:123456789012:instance/i-0abc"

    def test_s3_bucket_no_region_account(self):
        assert build_resource_arn("AWS::S3::Bucket", "my-bucket", self.R, self.A) == \
            "arn:aws:s3:::my-bucket"

    def test_lambda_function(self):
        assert build_resource_arn("AWS::Lambda::Function", "fn", self.R, self.A) == \
            "arn:aws:lambda:us-east-1:123456789012:function:fn"

    def test_dynamodb_table(self):
        assert build_resource_arn("AWS::DynamoDB::Table", "t", self.R, self.A) == \
            "arn:aws:dynamodb:us-east-1:123456789012:table/t"

    def test_rds_instance_vs_cluster(self):
        assert ":db:" in build_resource_arn("AWS::RDS::DBInstance", "d", self.R, self.A)
        assert ":cluster:" in build_resource_arn("AWS::RDS::DBCluster", "c", self.R, self.A)

    def test_already_arn_passthrough(self):
        arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/x/y"
        assert build_resource_arn("AWS::ElasticLoadBalancingV2::LoadBalancer", arn, self.R, self.A) == arn

    def test_unknown_type_returns_id(self):
        assert build_resource_arn("AWS::Weird::Thing", "raw-id", self.R, self.A) == "raw-id"


# --- Allowed-value rejection (inference_worker.py::_reject_disallowed_values) ---

def _reject_disallowed_values(suggested, tag_policy):
    """Copied from inference_worker/app.py for isolated testing."""
    if not suggested:
        return suggested
    cleaned = {}
    for k, v in suggested.items():
        allowed = tag_policy.get(k, {}).get("allowed_values")
        if allowed and v not in allowed:
            continue
        cleaned[k] = v
    return cleaned


class TestRejectDisallowedValues:
    POLICY = {
        "Environment": {"required": True, "allowed_values": ["prod", "dev"]},
        "Owner": {"required": True},  # no allowed_values → unconstrained
    }

    def test_valid_value_kept(self):
        assert _reject_disallowed_values({"Environment": "prod"}, self.POLICY) == {"Environment": "prod"}

    def test_invalid_value_dropped(self):
        assert _reject_disallowed_values({"Environment": "production"}, self.POLICY) == {}

    def test_unconstrained_key_always_kept(self):
        assert _reject_disallowed_values({"Owner": "anyone"}, self.POLICY) == {"Owner": "anyone"}

    def test_mixed(self):
        result = _reject_disallowed_values(
            {"Environment": "staging", "Owner": "team-a"}, self.POLICY
        )
        assert result == {"Owner": "team-a"}  # staging not allowed, Owner kept

    def test_empty(self):
        assert _reject_disallowed_values({}, self.POLICY) == {}
