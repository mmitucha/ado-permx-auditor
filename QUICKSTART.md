# Quick Start Guide

## Installation & Setup (2 minutes)

```bash
# 1. Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Create .env file
cat > .env << EOF
ADO_ORGANIZATION=your-org-name
ADO_PAT_TOKEN=your-pat-token
EOF

# 3. Run the auditor (using helper script)
./run_audit.sh

# OR run manually
export $(cat .env | grep -v '^#' | xargs) && uv run ado_permissions_auditor.py
```

## What You'll Get

All output files are in the `audit_output/` directory:

### Primary Output: CSV File
`audit_output/ado_permissions_audit_YYYYMMDD_HHMMSS.csv`

Contains all permission assignments with columns:
- Project, User, VSTS Group, Assignment Type, AAD Group Chain

### Log File
`audit_output/ado_audit_YYYYMMDD_HHMMSS.log`

Detailed execution log with:
- Progress updates
- Cache performance metrics
- Error messages (if any)

### Error Report (if errors occur)
`audit_output/ado_audit_errors_YYYYMMDD_HHMMSS.json`

Details about any API failures for troubleshooting

## Analyzing Results

```bash
# Generate analysis report
uv run analyze_permissions.py audit_output/ado_permissions_audit_*.csv audit_output/analysis_report.json
```

This provides insights on:
- Users with broad access
- AAD group reuse patterns
- Service principal inventory
- Direct vs AAD assignments
- Nested group structures

## Key Features

### 1. Advanced Caching
- **AAD groups:** Resolved once, reused everywhere (96%+ hit rate)
- **Users/SPs:** Cached by descriptor (94%+ hit rate)
- **Memberships:** Cached per project:group combination

### 2. Robust Error Handling
- Automatic retry with exponential backoff
- Rate limit detection and waiting
- Detailed error logging
- Graceful degradation

### 3. Performance
- **Concurrent processing:** 30 parallel requests by default
- **Batch processing:** 10 projects at a time
- **Memory efficient:** Streams to CSV, doesn't load all in RAM
- **Fast:** 2-4 hours for 1000 projects (vs 12+ hours naive)

## Estimated Runtime

| Projects | Groups | Estimated Time |
|----------|--------|----------------|
| 100      | ~1K    | 15-30 min      |
| 500      | ~5K    | 1-2 hours      |
| 1000     | ~10K   | 2-4 hours      |
| 2000     | ~20K   | 4-8 hours      |

*Assumes decent cache hit rates and no major rate limiting*

## Troubleshooting

### Rate Limited?
```python
# Reduce concurrent requests
max_concurrent=20  # or 10
```

### Out of Memory?
```python
# Reduce batch size
batch_size=5  # instead of 10
```

### Missing Permissions in Output?
- Check PAT token has correct permissions
- Review error log for 403/401 errors
- Some projects may have restricted access

### Slow Performance?
- Check network latency
- Verify cache hit rates in progress logs
- Consider running from Azure VM in same region

## PAT Token Permissions Required

Minimum required scopes:
- ✓ **Graph (Read)** - Read groups and members
- ✓ **Identity (Read)** - Read user details  
- ✓ **Project and Team (Read)** - List projects
- ✓ **User Profile (Read)** - User information

## Cache Performance Indicators

Good cache performance:
```
AAD groups: 90%+ hit rate     ✓ Excellent
Identities: 90%+ hit rate     ✓ Excellent
Group memberships: 40%+ hit   ✓ Good
```

Poor cache performance:
```
AAD groups: <70% hit rate     ⚠️ Investigate
Identities: <80% hit rate     ⚠️ Investigate
Group memberships: <20% hit   ⚠️ Expected if unique per project
```

## Common Use Cases

### 1. Security Audit
"Who has access to what?"
```bash
./run_audit.sh
# Review CSV for unexpected permissions
```

### 2. AAD Group Cleanup
"Which AAD groups are actually used?"
```bash
uv run analyze_permissions.py audit_output/audit.csv audit_output/report.json
# Review 'aad_groups.most_reused_groups' in report
```

### 3. Over-Privileged Users
"Who has admin access to many projects?"
```bash
uv run analyze_permissions.py audit_output/audit.csv audit_output/report.json
# Review 'user_access.potential_admins' in report
```

### 4. Service Principal Inventory
"What service accounts exist?"
```bash
uv run analyze_permissions.py audit_output/audit.csv audit_output/report.json
# Review 'service_principals' section
```

## Tips for Large Organizations (2000+ projects)

1. **Run during off-hours** to avoid impacting dev team
2. **Use tmux/screen** so process survives disconnection
3. **Monitor logs** to ensure progress
4. **Save intermediate results** (auto-flushed after each project)
5. **Plan for 8+ hours** for very large orgs

## Next Steps

1. Run the auditor
2. Review the CSV output
3. Generate analysis report
4. Identify any security concerns
5. Clean up unnecessary permissions
6. Schedule regular audits (quarterly recommended)

## Support

Check these files for more details:
- `README.md` - Full documentation
- `OPTIMIZATION_GUIDE.md` - Performance deep dive
- Log files - Troubleshooting information
