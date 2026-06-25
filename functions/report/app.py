# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Report Lambda — generates review CSV and human-readable summary.

Combines discovery stats with inference results to produce:
    1. A review CSV for human approval (approve/reject each suggestion)
    2. A plain-text summary with tier breakdown and projected compliance
    3. Optional SNS notification

Input (event):
    run_id (str): Pipeline run identifier
    region (str): AWS region

Output:
    run_id, CSV S3 key, summary text
"""

import json
import csv
import io
import logging
import boto3
from datetime import datetime, timezone

from config import RESULTS_BUCKET, RESULTS_PREFIX, REGION, SNS_TOPIC_ARN

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _build_json_data(recommendations):
    """Serialize recommendations to compact JSON for embedded JS table.

    Only includes fields needed for display — keeps HTML size bounded.
    At 10K resources this adds ~2MB to HTML (acceptable for browser).
    At 50K+ we cap at 10K rows to keep the file under 5MB.
    """
    MAX_EMBEDDED = 10000
    items = []
    for r in recommendations[:MAX_EMBEDDED]:
        inf = r.get("inference", {})
        suggested = inf.get("suggested_tags", {})
        tags_str = ",".join(f"{k}: {v}" for k, v in suggested.items()) if suggested else ""
        items.append({
            "arn": r.get("arn", ""),
            "type": r.get("resource_type", ""),
            "tier": inf.get("tier", 5),
            "conf": inf.get("confidence", 0),
            "tags": tags_str,
            "hasSuggestion": bool(suggested),
            "evidence": inf.get("evidence", "")[:150],
            "orphan": inf.get("is_likely_orphan", False),
        })
    return json.dumps(items, separators=(",", ":"))


def _generate_html_report(
    run_id, region, summary_stats, tier_counts, orphan_count,
    projected_pct, recommendations, bedrock_warning,
) -> str:
    """Generate enterprise HTML report grouped by action priority."""
    total = summary_stats["total_resources"]
    compliant = summary_stats["compliant"]
    compliance_pct = summary_stats["compliance_pct"]

    # Categorize recommendations into action groups
    auto_apply = [r for r in recommendations if r.get("inference", {}).get("tier") in (1, 2) and r.get("inference", {}).get("suggested_tags")]
    high_confidence = [r for r in recommendations if r.get("inference", {}).get("tier") == 3 and r.get("inference", {}).get("suggested_tags")]
    ai_suggestions = [r for r in recommendations if r.get("inference", {}).get("tier") == 4 and r.get("inference", {}).get("suggested_tags")]
    needs_review = [r for r in recommendations if r.get("inference", {}).get("tier") == 5 or (r.get("inference", {}).get("tier") == 4 and not r.get("inference", {}).get("suggested_tags"))]
    orphans = [r for r in recommendations if r.get("inference", {}).get("is_likely_orphan")]

    # Group by application (infer from Name tag, stack, or VPC)
    def _app_group(rec):
        tags = rec.get("tags", {})
        # Priority: stack name > application tag > Name pattern > resource type
        if tags.get("aws:cloudformation:stack-name"):
            return tags["aws:cloudformation:stack-name"]
        for k in ("Application", "application", "app", "App", "Project", "project"):
            if k in tags:
                return tags[k]
        name = tags.get("Name", "")
        if name:
            # Extract prefix pattern: "payment-gateway-prod-xyz" → "payment-gateway"
            parts = name.replace("_", "-").split("-")
            if len(parts) >= 2:
                return "-".join(parts[:2])
            return name
        return f"ungrouped-{rec.get('resource_type', 'unknown')}"

    # Build application groups for AI suggestions
    app_groups = {}
    for rec in ai_suggestions:
        group = _app_group(rec)
        app_groups.setdefault(group, []).append(rec)

    # Resource type breakdown
    from collections import Counter
    type_counts = Counter(r.get("resource_type", "unknown") for r in recommendations)
    top_types = type_counts.most_common(8)

    # Tag gap analysis: which required tags are most commonly missing?
    missing_tag_counts = Counter()
    for r in recommendations:
        for tag in r.get("missing_tags", []):
            missing_tag_counts[tag] += 1
    top_missing = missing_tag_counts.most_common(5)

    # Helper to render a resource row
    def _row(rec, show_evidence=True):
        inf = rec.get("inference", {})
        tier = inf.get("tier", 5)
        tier_colors = {1: "#2E7D32", 2: "#1565C0", 3: "#F57C00", 4: "#6A1B9A", 5: "#C62828"}
        color = tier_colors.get(tier, "#666")
        suggested = inf.get("suggested_tags", {})
        tags_html = "".join(
            f'<span class="tag-pill">{k}: <b>{v}</b></span>' for k, v in suggested.items()
        ) if suggested else '<span class="no-suggestion">No suggestion</span>'
        orphan_badge = ' <span class="orphan-badge">ORPHAN?</span>' if inf.get("is_likely_orphan") else ""
        arn_short = rec["arn"].split(":")[-1][:50]
        confidence = inf.get("confidence", 0)
        conf_class = "conf-high" if confidence >= 80 else "conf-mid" if confidence >= 50 else "conf-low"
        evidence_col = f'<td class="evidence">{inf.get("evidence","")[:120]}</td>' if show_evidence else ""
        return f"""<tr>
            <td class="arn" title="{rec['arn']}">{arn_short}</td>
            <td class="rtype">{rec.get('resource_type','')}</td>
            <td class="tier-cell"><span class="tier-badge" style="background:{color}">T{tier}</span></td>
            <td class="conf-cell"><span class="{conf_class}">{confidence}%</span></td>
            <td>{tags_html}{orphan_badge}</td>
            {evidence_col}
        </tr>"""

    # Build application group sections
    app_sections = ""
    for group_name in sorted(app_groups.keys(), key=lambda g: -len(app_groups[g])):
        items = app_groups[group_name]
        # Show common suggested tags across group
        common_tags = Counter()
        for r in items:
            for k, v in r.get("inference", {}).get("suggested_tags", {}).items():
                common_tags[f"{k}={v}"] += 1
        common_pills = "".join(
            f'<span class="tag-pill">{tag.split("=")[0]}: <b>{tag.split("=")[1]}</b></span> '
            for tag, cnt in common_tags.most_common(4) if cnt > 1
        )
        rows = "".join(_row(r, show_evidence=False) for r in items[:10])
        overflow = f'<tr><td colspan="5" class="overflow">+ {len(items)-10} more in this group (see CSV)</td></tr>' if len(items) > 10 else ""
        app_sections += f"""
        <div class="app-group">
            <div class="app-header">
                <span class="app-name">{group_name}</span>
                <span class="app-count">{len(items)} resources</span>
                {f'<div class="app-common">Common: {common_pills}</div>' if common_pills else ''}
            </div>
            <table class="compact-table">
                <tr><th>Resource</th><th>Type</th><th>Tier</th><th>Conf</th><th>Suggested Tags</th></tr>
                {rows}{overflow}
            </table>
        </div>"""

    # Auto-apply rows
    auto_rows = "".join(_row(r) for r in auto_apply[:20])
    # Orphan rows
    orphan_rows = "".join(_row(r) for r in orphans[:10])

    # Needs review — show WHY (what's missing)
    review_rows = ""
    for rec in needs_review[:20]:
        inf = rec.get("inference", {})
        arn_short = rec["arn"].split(":")[-1][:45]
        reasons = []
        tags = rec.get("tags", {})
        if not tags.get("Name") and not any(not k.startswith("aws:") for k in tags):
            reasons.append("No tags at all")
        elif not tags.get("Name"):
            reasons.append("No Name tag")
        if inf.get("evidence", "").startswith("No CloudTrail"):
            reasons.append("No CloudTrail history")
        if not reasons:
            reasons.append("Insufficient signal")
        review_rows += f"""<tr>
            <td class="arn" title="{rec['arn']}">{arn_short}</td>
            <td class="rtype">{rec.get('resource_type','')}</td>
            <td>{'<br>'.join(reasons)}</td>
            <td>{', '.join(rec.get('missing_tags',[])[:3])}</td>
        </tr>"""

    warning_html = f'<div class="warning">{bedrock_warning}</div>' if bedrock_warning else ""

    # Resource type chart (simple bar)
    type_bars = "".join(
        f'<div class="type-bar"><span class="type-label">{t}</span><div class="bar" style="width:{int(c/max(1,type_counts.most_common(1)[0][1])*100)}%"><span>{c}</span></div></div>'
        for t, c in top_types
    )

    # Missing tag chart
    missing_bars = "".join(
        f'<div class="type-bar"><span class="type-label">{t}</span><div class="bar bar-red" style="width:{int(c/max(1,missing_tag_counts.most_common(1)[0][1])*100)}%"><span>{c}</span></div></div>'
        for t, c in top_missing
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>TagSense — {run_id}</title>
<style>
:root {{ --aws-dark: #232F3E; --aws-orange: #FF9900; --green: #2E7D32; --blue: #1565C0; --orange: #F57C00; --purple: #6A1B9A; --red: #C62828; }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1300px; margin: 0 auto; padding: 24px; background: #F5F6FA; color: #333; }}
h1 {{ color: var(--aws-dark); border-bottom: 3px solid var(--aws-orange); padding-bottom: 10px; margin-bottom: 4px; }}
h2 {{ color: var(--aws-dark); margin-top: 32px; margin-bottom: 12px; font-size: 18px; }}
.subtitle {{ color: #666; font-size: 13px; margin-bottom: 24px; }}

/* Executive cards */
.exec-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 24px 0; }}
.exec-card {{ background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; }}
.exec-card .value {{ font-size: 36px; font-weight: 700; }}
.exec-card .label {{ font-size: 12px; color: #666; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.5px; }}
.exec-card.highlight {{ border: 2px solid var(--aws-orange); }}
.exec-card .subtext {{ font-size: 11px; color: #999; margin-top: 4px; }}

/* Action sections */
.action-section {{ background: white; border-radius: 12px; padding: 20px 24px; margin: 20px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
.action-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
.action-badge {{ padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; color: white; }}

/* Tables */
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ background: var(--aws-dark); color: white; padding: 10px 8px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
td {{ padding: 10px 8px; border-bottom: 1px solid #EEE; vertical-align: middle; }}
tr:hover {{ background: #F8F9FA; }}
.compact-table th {{ background: #F0F0F0; color: #333; }}
.compact-table td {{ padding: 8px 6px; font-size: 11px; }}

/* Tags */
.tag-pill {{ display: inline-block; background: #E8F5E9; border: 1px solid #A5D6A7; padding: 2px 8px; border-radius: 12px; margin: 2px; font-size: 11px; }}
.no-suggestion {{ color: #999; font-style: italic; font-size: 11px; }}
.orphan-badge {{ background: #FFCDD2; padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; }}
.tier-badge {{ color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; }}

/* Confidence */
.conf-high {{ color: var(--green); font-weight: 700; }}
.conf-mid {{ color: var(--orange); font-weight: 700; }}
.conf-low {{ color: var(--red); font-weight: 700; }}
.conf-cell {{ text-align: center; }}
.tier-cell {{ text-align: center; }}

/* Cells */
.arn {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px; color: #444; }}
.rtype {{ font-size: 11px; color: #666; }}
.evidence {{ font-size: 10px; color: #888; max-width: 220px; }}
.overflow {{ text-align: center; color: #999; font-style: italic; padding: 8px; }}

/* App groups */
.app-group {{ border: 1px solid #E0E0E0; border-radius: 8px; padding: 12px; margin: 12px 0; }}
.app-header {{ margin-bottom: 8px; }}
.app-name {{ font-weight: 700; font-size: 14px; color: var(--aws-dark); }}
.app-count {{ background: #E3F2FD; color: var(--blue); padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-left: 8px; }}
.app-common {{ margin-top: 4px; }}

/* Charts */
.chart-container {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin: 16px 0; }}
.chart-box {{ background: white; padding: 16px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
.type-bar {{ display: flex; align-items: center; margin: 4px 0; }}
.type-label {{ width: 160px; font-size: 11px; color: #555; text-align: right; padding-right: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.bar {{ background: linear-gradient(90deg, #42A5F5, #1565C0); border-radius: 3px; padding: 3px 8px; color: white; font-size: 10px; min-width: 30px; }}
.bar-red {{ background: linear-gradient(90deg, #EF5350, #C62828); }}

/* Warning */
.warning {{ background: #FFF3E0; border-left: 4px solid var(--orange); padding: 14px 18px; margin: 16px 0; border-radius: 4px; font-size: 13px; }}

/* Footer */
.footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #DDD; color: #999; font-size: 11px; text-align: center; }}
code {{ background: #F0F0F0; padding: 2px 6px; border-radius: 3px; font-size: 11px; }}
</style></head><body>

<h1>🏷️ TagSense Compliance Report</h1>
<p class="subtitle">Run <b>{run_id}</b> &middot; {region} &middot; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>

{warning_html}

<!-- Executive Summary -->
<div class="exec-grid">
    <div class="exec-card">
        <div class="value">{total}</div>
        <div class="label">Total Resources</div>
    </div>
    <div class="exec-card">
        <div class="value" style="color:var(--blue)">{summary_stats.get('iac_coverage_pct', 'N/A')}%</div>
        <div class="label">IaC Coverage</div>
        <div class="subtext">{summary_stats.get('iac_managed', 0)} managed, {summary_stats.get('iac_unmanaged', 0)} unmanaged</div>
    </div>
    <div class="exec-card">
        <div class="value" style="color:var(--red)">{compliance_pct}%</div>
        <div class="label">Current Compliance</div>
        <div class="subtext">{compliant} of {total} fully tagged</div>
    </div>
    <div class="exec-card highlight">
        <div class="value" style="color:var(--green)">{projected_pct}%</div>
        <div class="label">Projected Compliance</div>
        <div class="subtext">If all suggestions applied</div>
    </div>
    <div class="exec-card">
        <div class="value" style="color:var(--green)">{len(auto_apply)}</div>
        <div class="label">Quick Wins</div>
        <div class="subtext">Auto-apply (T1+T2, ≥95% conf)</div>
    </div>
    <div class="exec-card">
        <div class="value" style="color:var(--purple)">{len(ai_suggestions)}</div>
        <div class="label">AI Suggestions</div>
        <div class="subtext">Review recommended</div>
    </div>
    <div class="exec-card">
        <div class="value" style="color:{'var(--red)' if orphan_count else '#999'}">{orphan_count}</div>
        <div class="label">Orphan Candidates</div>
        <div class="subtext">Potential cost savings</div>
    </div>
</div>

<!-- Insights -->
<div class="chart-container">
    <div class="chart-box">
        <h3 style="margin-top:0;font-size:14px;">Resources by Type</h3>
        {type_bars}
    </div>
    <div class="chart-box">
        <h3 style="margin-top:0;font-size:14px;">Most Common Missing Tags</h3>
        {missing_bars}
    </div>
</div>

<!-- Section 1: Auto-Apply -->
{'<div class="action-section"><div class="action-header"><span class="action-badge" style="background:var(--green)">AUTO-APPLY</span><span>High-confidence suggestions from CloudFormation stacks and CloudTrail — safe to apply without review</span></div><table><tr><th>Resource</th><th>Type</th><th>Tier</th><th>Conf</th><th>Suggested Tags</th><th>Evidence</th></tr>' + auto_rows + '</table></div>' if auto_apply else ''}

<!-- Section 2: AI Suggestions grouped by application -->
{'<div class="action-section"><div class="action-header"><span class="action-badge" style="background:var(--purple)">REVIEW — AI SUGGESTIONS</span><span>Bedrock inferred these from resource names and context. Grouped by application for batch review.</span></div>' + app_sections + '</div>' if ai_suggestions else ''}

<!-- Section 3: Orphans -->
{'<div class="action-section"><div class="action-header"><span class="action-badge" style="background:var(--red)">ORPHAN CANDIDATES</span><span>Resources with no tags, no recent activity, and no identifiable owner. Consider decommissioning.</span></div><table><tr><th>Resource</th><th>Type</th><th>Tier</th><th>Conf</th><th>Suggested Tags</th><th>Evidence</th></tr>' + orphan_rows + '</table></div>' if orphans else ''}

<!-- Section 4: Needs Human -->
<div class="action-section">
    <div class="action-header">
        <span class="action-badge" style="background:var(--red)">NEEDS HUMAN REVIEW</span>
        <span>{len(needs_review)} resources with insufficient signal for automated inference</span>
    </div>
    <table>
        <tr><th>Resource</th><th>Type</th><th>Why No Suggestion</th><th>Missing Tags</th></tr>
        {review_rows}
    </table>
    {'<p class="overflow">+ ' + str(len(needs_review)-20) + ' more (see CSV)</p>' if len(needs_review) > 20 else ''}
</div>

<!-- Full Interactive Table -->
<div class="action-section">
    <h2 style="margin-top:0;">All Resources (Interactive)</h2>
    <div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap;align-items:center;">
        <input type="text" id="search" placeholder="Search ARN, type, or tags..." style="padding:8px 12px;border:1px solid #DDD;border-radius:6px;width:280px;font-size:13px;">
        <select id="tierFilter" style="padding:8px;border:1px solid #DDD;border-radius:6px;font-size:13px;">
            <option value="">All Tiers</option>
            <option value="1">Tier 1 — CloudFormation</option>
            <option value="2">Tier 2 — CloudTrail</option>
            <option value="3">Tier 3 — Neighbor</option>
            <option value="4">Tier 4 — Bedrock AI</option>
            <option value="5">Tier 5 — Manual</option>
        </select>
        <select id="typeFilter" style="padding:8px;border:1px solid #DDD;border-radius:6px;font-size:13px;">
            <option value="">All Types</option>
        </select>
        <label style="font-size:12px;"><input type="checkbox" id="sugOnly"> With suggestions only</label>
        <span id="resultCount" style="font-size:12px;color:#666;margin-left:auto;"></span>
    </div>
    <div id="tableContainer"></div>
    <div id="pagination" style="display:flex;justify-content:center;gap:8px;margin-top:12px;"></div>
</div>

<!-- Next Steps -->
<div class="action-section">
    <h2 style="margin-top:0;">Next Steps</h2>
    <ol style="line-height:2.2;font-size:13px;">
        <li><b>Quick wins:</b> Apply Tier 1+2 suggestions immediately — run <code>Apply Lambda</code> with <code>dry_run: true</code>, then <code>dry_run: false</code></li>
        <li><b>Review AI suggestions:</b> Download <code>review.csv</code>, filter Tier 4, mark "Y" in Approve column</li>
        <li><b>Investigate orphans:</b> Verify orphan candidates with team owners before cleanup</li>
        <li><b>Improve future scans:</b> Add <code>org-context.json</code> to guide AI, enrich resource Name tags</li>
    </ol>
    <p style="font-size:12px;color:#666;">Download full CSV: <code>aws s3 cp s3://{RESULTS_BUCKET}/{RESULTS_PREFIX}/{run_id}/review.csv ./review.csv</code></p>
</div>

<div class="footer">
    TagSense — AI-Powered Tag Debt Remediation &middot; Tiers 1-2 are deterministic, Tier 4 uses Amazon Bedrock (Claude) &middot; Confidence scores reflect method reliability, not certainty.
</div>

<script>
const DATA = {_build_json_data(recommendations)};
const PAGE_SIZE = 50;
let currentPage = 1;
let filtered = DATA;

// Populate type filter
const types = [...new Set(DATA.map(r => r.type))].sort();
const tf = document.getElementById('typeFilter');
types.forEach(t => {{ const o = document.createElement('option'); o.value = t; o.textContent = t; tf.appendChild(o); }});

function applyFilters() {{
    const q = document.getElementById('search').value.toLowerCase();
    const tier = document.getElementById('tierFilter').value;
    const type = document.getElementById('typeFilter').value;
    const sugOnly = document.getElementById('sugOnly').checked;
    filtered = DATA.filter(r => {{
        if (q && !r.arn.toLowerCase().includes(q) && !r.type.toLowerCase().includes(q) && !r.tags.toLowerCase().includes(q)) return false;
        if (tier && r.tier != tier) return false;
        if (type && r.type !== type) return false;
        if (sugOnly && !r.hasSuggestion) return false;
        return true;
    }});
    currentPage = 1;
    render();
}}

function render() {{
    const start = (currentPage - 1) * PAGE_SIZE;
    const page = filtered.slice(start, start + PAGE_SIZE);
    const tc = {{"1":"#2E7D32","2":"#1565C0","3":"#F57C00","4":"#6A1B9A","5":"#C62828"}};
    let html = '<table><tr><th>Resource</th><th>Type</th><th>Tier</th><th>Conf</th><th>Suggested Tags</th><th>Evidence</th></tr>';
    page.forEach(r => {{
        const color = tc[r.tier] || '#666';
        const cc = r.conf >= 80 ? 'conf-high' : r.conf >= 50 ? 'conf-mid' : 'conf-low';
        const tags = r.tags ? r.tags.split(',').map(t => '<span class="tag-pill">' + t + '</span>').join('') : '<span class="no-suggestion">—</span>';
        html += `<tr><td class="arn" title="${{r.arn}}">${{r.arn.split(':').pop().slice(0,50)}}</td><td class="rtype">${{r.type}}</td><td class="tier-cell"><span class="tier-badge" style="background:${{color}}">T${{r.tier}}</span></td><td class="conf-cell"><span class="${{cc}}">${{r.conf}}%</span></td><td>${{tags}}</td><td class="evidence">${{r.evidence.slice(0,120)}}</td></tr>`;
    }});
    html += '</table>';
    document.getElementById('tableContainer').innerHTML = html;
    document.getElementById('resultCount').textContent = filtered.length + ' of ' + DATA.length + ' resources';

    // Pagination
    const pages = Math.ceil(filtered.length / PAGE_SIZE);
    let pg = '';
    if (pages > 1) {{
        pg += `<button onclick="goPage(1)" ${{currentPage===1?'disabled':''}}>«</button>`;
        for (let i = Math.max(1,currentPage-2); i <= Math.min(pages,currentPage+2); i++) {{
            pg += `<button onclick="goPage(${{i}})" style="${{i===currentPage?'background:var(--aws-dark);color:white':''}}">${{i}}</button>`;
        }}
        pg += `<button onclick="goPage(${{pages}})" ${{currentPage===pages?'disabled':''}}>»</button>`;
    }}
    document.getElementById('pagination').innerHTML = pg;
}}

function goPage(p) {{ currentPage = p; render(); }}

document.getElementById('search').addEventListener('input', applyFilters);
document.getElementById('tierFilter').addEventListener('change', applyFilters);
document.getElementById('typeFilter').addEventListener('change', applyFilters);
document.getElementById('sugOnly').addEventListener('change', applyFilters);
render();
</script>
</body></html>"""


def handler(event, context):
    """Lambda entry point — generates report and review CSV."""
    run_id = event["run_id"]
    region = event.get("region", REGION)

    logger.info("Generating report for run_id=%s", run_id)

    s3 = boto3.client("s3")

    # Load discovery and inference results
    # The Report Lambda is the single merge point — reads all intermediate outputs
    # and produces the canonical inference.json + CSV + HTML.
    discovery = json.loads(
        s3.get_object(
            Bucket=RESULTS_BUCKET,
            Key=f"{RESULTS_PREFIX}/{run_id}/discovery.json",
        )["Body"].read()
    )

    # Read tier 1-3 results from Distributed Map aggregation
    try:
        tier123 = json.loads(
            s3.get_object(
                Bucket=RESULTS_BUCKET,
                Key=f"{RESULTS_PREFIX}/{run_id}/tier123_results.json",
            )["Body"].read()
        )
    except Exception:
        tier123 = []

    # Read Bedrock batch/real-time results (if exists — may have been written by
    # Poller or Bedrock Batch Lambda's real-time fallback)
    try:
        bedrock_inference = json.loads(
            s3.get_object(
                Bucket=RESULTS_BUCKET,
                Key=f"{RESULTS_PREFIX}/{run_id}/inference.json",
            )["Body"].read()
        )
        bedrock_recs = bedrock_inference.get("recommendations", [])
    except Exception:
        bedrock_recs = []

    # Merge: tier123 has ALL resources with their inference results.
    # Bedrock recs may have updated Tier 4 resources with actual suggestions.
    # Build a lookup from Bedrock results (keyed by ARN) to overlay onto tier123.
    bedrock_by_arn = {
        r["arn"]: r for r in bedrock_recs
        if r.get("inference", {}).get("suggested_tags")
    }

    # Merge: for each tier123 resource, if Bedrock produced a better result, use it
    all_recommendations = []
    for resource in tier123:
        arn = resource.get("arn", "")
        if arn in bedrock_by_arn:
            # Bedrock produced suggestions — use its result
            all_recommendations.append(bedrock_by_arn[arn])
        elif resource.get("inference", {}).get("suggested_tags") or resource.get("inference", {}).get("tier") in (1, 2, 3):
            # Tier 1/2/3 resolved — keep as-is
            all_recommendations.append(resource)
        else:
            # Unresolved (Tier 4 pending or Tier 5)
            all_recommendations.append(resource)

    # Also include any Bedrock results for ARNs not in tier123 (edge case)
    tier123_arns = {r.get("arn") for r in tier123}
    for arn, rec in bedrock_by_arn.items():
        if arn not in tier123_arns:
            all_recommendations.append(rec)

    # Write the canonical merged inference.json
    merged_output = {
        "run_id": run_id,
        "region": region,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_inferred": len(all_recommendations),
        "recommendations": all_recommendations,
    }
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=f"{RESULTS_PREFIX}/{run_id}/inference.json",
        Body=json.dumps(merged_output, default=str),
        ContentType="application/json",
    )

    inference = merged_output

    # Detect unresolved Tier 4 resources
    tier4_no_suggestion = sum(
        1 for r in all_recommendations
        if r.get("inference", {}).get("tier") == 4
        and not r.get("inference", {}).get("suggested_tags")
    )
    tier4_with_suggestion = sum(
        1 for r in all_recommendations
        if r.get("inference", {}).get("tier") == 4
        and r.get("inference", {}).get("suggested_tags")
    )
    bedrock_warning = ""
    if tier4_no_suggestion > 0:
        if tier4_with_suggestion > 0:
            # Batch worked but model couldn't infer for some resources
            bedrock_warning = (
                f"⚠ {tier4_no_suggestion} resources were sent to AI but received no confident suggestions. "
                f"({tier4_with_suggestion} resources did receive suggestions.) "
                "These resources likely lack sufficient naming conventions or context for the model to infer values. "
                "Consider adding an org-context.json steering file or enriching resource Name tags."
            )
        else:
            # No suggestions at all — likely a batch/model issue
            bedrock_warning = (
                f"⚠ {tier4_no_suggestion} resources were sent to AI but received no suggestions. "
                "Possible causes: Bedrock model not enabled, batch job failed, or IAM role "
                "missing permissions. Check /aws/lambda/{stack}-bedrock-batch logs."
            )
        logger.warning(bedrock_warning)

    # Generate review CSV for human approval workflow
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow([
        "ARN", "Resource Type", "Existing Tags", "Missing Tags",
        "Suggested Tags", "Confidence %", "Tier", "Method",
        "Evidence", "Likely Orphan", "Approve (Y/N)",
    ])

    for rec in inference.get("recommendations", []):
        inf = rec.get("inference", {})
        # Fix stale evidence for unresolved Tier 4
        evidence = inf.get("evidence", "N/A")
        if (inf.get("tier") == 4 and not inf.get("suggested_tags")
                and evidence == "Queued for AI inference"):
            evidence = "Bedrock Batch did not return results — check model access and batch job logs"

        writer.writerow([
            rec["arn"],
            rec["resource_type"],
            json.dumps(rec.get("tags", {})),
            ", ".join(rec.get("missing_tags", [])),
            json.dumps(inf.get("suggested_tags", {})),
            inf.get("confidence", 0),
            inf.get("tier", "N/A"),
            inf.get("method", "N/A"),
            evidence,
            "YES" if inf.get("is_likely_orphan") else "",
            "Y" if inf.get("tier") in (1, 2) and inf.get("suggested_tags") else "N",
        ])

    csv_key = f"{RESULTS_PREFIX}/{run_id}/review.csv"
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=csv_key,
        Body=csv_buffer.getvalue(),
        ContentType="text/csv",
    )

    # Compute tier breakdown from actual results
    summary_stats = discovery["summary"]
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for rec in inference.get("recommendations", []):
        tier = rec.get("inference", {}).get("tier", 5)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    orphan_count = sum(
        1 for r in inference.get("recommendations", [])
        if r.get("inference", {}).get("is_likely_orphan")
    )

    # Project compliance improvement if all suggestions are applied
    resolvable = sum(tier_counts.get(t, 0) for t in [1, 2, 3, 4])
    total = summary_stats["total_resources"]
    projected_pct = (
        round((summary_stats["compliant"] + resolvable) / total * 100, 1)
        if total
        else 0
    )

    # Calculate quick wins
    quick_wins = tier_counts.get(1, 0) + tier_counts.get(2, 0)
    ai_suggestions = tier_counts.get(4, 0)
    total_suggestions = quick_wins + tier_counts.get(3, 0) + ai_suggestions

    summary_text = f"""TagSense Scan Complete ✓
{'='*50}
Run ID: {run_id} | {datetime.now(timezone.utc).strftime('%B %d, %Y at %H:%M UTC')} | Region: {region}

Hi — here's what TagSense found in your account:

📊 YOUR ACCOUNT AT A GLANCE
   {summary_stats['total_resources']} total resources scanned
   {summary_stats['compliant']} already compliant ({summary_stats['compliance_pct']}%)
   {summary_stats['non_compliant']} need tagging attention
   {summary_stats.get('iac_managed', 0)} managed by IaC ({summary_stats.get('iac_coverage_pct', 0)}% coverage)

🏷️  WHAT TAGSENSE RECOMMENDS
   TagSense analyzed your {summary_stats['non_compliant']} non-compliant resources and produced
   {total_suggestions} tag suggestions across {sum(1 for t in [1,2,3,4] if tier_counts.get(t,0) > 0)} confidence tiers:

   ✅ {quick_wins} high-confidence (Tier 1-2) — safe to apply immediately
      These come from CloudFormation stack tags and CloudTrail creator attribution.

   🔶 {tier_counts.get(3, 0)} medium-confidence (Tier 3) — from VPC neighbor consensus
      Verify these make sense for the specific resource before applying.

   🤖 {ai_suggestions} AI-inferred (Tier 4) — Bedrock suggestions with evidence
      Review the reasoning in the CSV before approving.

   ✋ {tier_counts.get(5, 0)} need human review (Tier 5) — insufficient signal for automation
      These resources lack enough context for TagSense to suggest values.

   🗑️  {orphan_count} orphan candidate(s) — no recent activity or owner signals
{f'   ⚠️  {bedrock_warning}' if bedrock_warning else ''}

📈 PROJECTED IMPACT
   If you approve all suggestions, compliance improves from {summary_stats["compliance_pct"]}% → {projected_pct}%

🚀 WHAT TO DO NEXT
   1. Review the HTML report (visual dashboard):
      aws s3 cp s3://{RESULTS_BUCKET}/{RESULTS_PREFIX}/{run_id}/report.html ./report.html
      open report.html

   2. Download the review CSV:
      aws s3 cp s3://{RESULTS_BUCKET}/{csv_key} ./review.csv

   3. Open in Excel/Sheets → filter by Tier → mark "Y" in Approve column

   4. Apply approved tags (dry-run first):
      aws lambda invoke --function-name tagsense-apply \\
        --payload '{{"run_id": "{run_id}", "dry_run": true}}' response.json

📁 FILES
   HTML Report: s3://{RESULTS_BUCKET}/{RESULTS_PREFIX}/{run_id}/report.html
   Review CSV:  s3://{RESULTS_BUCKET}/{csv_key}
   Raw data:    s3://{RESULTS_BUCKET}/{RESULTS_PREFIX}/{run_id}/inference.json
"""

    # Save summary
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=f"{RESULTS_PREFIX}/{run_id}/summary.txt",
        Body=summary_text,
        ContentType="text/plain",
    )

    # Generate HTML report for easier consumption
    html_key = f"{RESULTS_PREFIX}/{run_id}/report.html"
    html_report = _generate_html_report(
        run_id, region, summary_stats, tier_counts, orphan_count,
        projected_pct, inference.get("recommendations", []), bedrock_warning,
    )
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=html_key,
        Body=html_report,
        ContentType="text/html",
    )

    # Send SNS notification if configured
    if SNS_TOPIC_ARN:
        try:
            boto3.client("sns", region_name=REGION).publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=f"TagSense Report — {run_id}",
                Message=summary_text,
            )
        except Exception as e:
            logger.error("Failed to publish SNS notification: %s", e)

    # Publish CloudWatch custom metrics for observability
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        cw.put_metric_data(
            Namespace="TagSense",
            MetricData=[
                {"MetricName": "ResourcesScanned", "Value": summary_stats["total_resources"], "Unit": "Count"},
                {"MetricName": "CompliancePercent", "Value": summary_stats["compliance_pct"], "Unit": "Percent"},
                {"MetricName": "ProjectedCompliancePercent", "Value": projected_pct, "Unit": "Percent"},
                {"MetricName": "OrphanCandidates", "Value": orphan_count, "Unit": "Count"},
                {"MetricName": "Tier1Resolved", "Value": tier_counts.get(1, 0), "Unit": "Count"},
                {"MetricName": "Tier2Resolved", "Value": tier_counts.get(2, 0), "Unit": "Count"},
                {"MetricName": "Tier3Resolved", "Value": tier_counts.get(3, 0), "Unit": "Count"},
                {"MetricName": "Tier4Resolved", "Value": tier_counts.get(4, 0), "Unit": "Count"},
                {"MetricName": "Tier5Manual", "Value": tier_counts.get(5, 0), "Unit": "Count"},
            ],
        )
    except Exception as e:
        logger.error("Failed to publish CloudWatch metrics: %s", e)

    logger.info("Report generated: csv=%s, projected_compliance=%.1f%%", csv_key, projected_pct)

    # Print output locations for CLI users
    output_msg = (
        f"\n{'='*50}\n"
        f"✅ TagSense scan complete — {run_id}\n"
        f"{'='*50}\n"
        f"\nOutputs ready:\n"
        f"  📄 HTML Report:  aws s3 cp s3://{RESULTS_BUCKET}/{RESULTS_PREFIX}/{run_id}/report.html ./report.html && open report.html\n"
        f"  📋 Review CSV:   aws s3 cp s3://{RESULTS_BUCKET}/{csv_key} ./review.csv\n"
        f"  📝 Summary:      aws s3 cp s3://{RESULTS_BUCKET}/{RESULTS_PREFIX}/{run_id}/summary.txt - --region {region}\n"
    )
    logger.info(output_msg)

    return {"run_id": run_id, "csv_key": csv_key, "summary": summary_text, "output_message": output_msg}
