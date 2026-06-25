# Changelog

## [1.0.0] - 2026-06-14

### Added
- 5-tier inference engine (CloudFormation → CloudTrail → VPC Neighbor → Bedrock AI → Manual)
- Bedrock Batch inference with real-time parallel fallback
- Extended thinking retry for low-confidence resources
- Interactive HTML report with search, filter, pagination
- Review CSV with approval workflow
- Org-context steering document support
- EventBridge scheduled scans
- CloudWatch metrics and DLQ for observability
- Signal-quality gating to prevent low-confidence suggestions
- Orphan resource detection
