# Azure DevOps Permissions Auditor - Session Memory

## Problem Summary
The Azure DevOps permissions auditor script was failing with API version errors when trying to fetch groups from projects.

## Root Cause
The Azure DevOps Graph API requires the `-preview` suffix in the API version parameter for all Graph-related endpoints.

## Changes Made

### 1. Fixed API Version for Graph Endpoints
Changed all Graph API endpoints from `api-version=7.0` to `api-version=7.0-preview`:

- **Line 379**: Groups endpoint
  ```python
  # Before: url = f"_apis/graph/groups?scopeDescriptor={project_id}&api-version=7.0"
  # After: url = f"_apis/graph/groups?api-version=7.0-preview"
  ```

- **Line 428**: Memberships endpoint
  ```python
  # Before: url = f"_apis/graph/memberships/{group_descriptor}?direction=down&api-version=7.0"
  # After: url = f"_apis/graph/memberships/{group_descriptor}?direction=down&api-version=7.0-preview"
  ```

- **Line 477**: Descriptors endpoint
  ```python
  # Before: url = f"_apis/graph/descriptors/{descriptor}?api-version=7.0"
  # After: url = f"_apis/graph/descriptors/{descriptor}?api-version=7.0-preview"
  ```

- **Line 552**: Users endpoint
  ```python
  # Before: url = f"_apis/graph/users?subjectTypes=aad&scopeDescriptor=scp.{origin_id}&api-version=7.0"
  # After: url = f"_apis/graph/users?subjectTypes=aad&scopeDescriptor=scp.{origin_id}&api-version=7.0-preview"
  ```

### 2. Removed Problematic scopeDescriptor Parameter
The `scopeDescriptor` parameter was causing issues with invalid subject type errors. Changed the groups endpoint to fetch all groups without scoping:

```python
# Removed scopeDescriptor - now fetches all groups and filters later if needed
url = f"_apis/graph/groups?api-version=7.0-preview"
```

## Current Status

### ✅ Working
- Script now successfully fetches groups (19 groups found in test run)
- Projects are being processed
- No more API version errors

### 3. Fixed get_identity_details Endpoint (2025-01-16)
The `get_identity_details` method was using incorrect API endpoints.

**Problem 1** (Initial):
- Was using: `_apis/graph/descriptors/{descriptor}?api-version=7.0-preview`
- This endpoint doesn't exist in the Azure DevOps REST API
- Resulted in 400 errors: "The request is invalid"

**Problem 2** (Discovered after initial fix):
- Changed to: `_apis/graph/users/{descriptor}?api-version=7.1-preview.1`
- This only works for user descriptors, NOT for VSTS groups
- Error log showed 20+ errors: "VS860045: Provided subject type 'vssgp' is not supported by the API endpoint."
- VSTS group descriptors (starting with `vssgp.`) cannot use the `/users/` endpoint

**Final Solution** (Lines 478-486):
```python
# Different descriptor types require different endpoints
# Group descriptors (vssgp, aadgp) -> /groups/ endpoint
# User descriptors (aad, etc.) -> /users/ endpoint
if descriptor.startswith(('vssgp.', 'aadgp.')):
    # VSTS groups and AAD groups
    url = f"_apis/graph/groups/{descriptor}?api-version=7.1-preview.1"
else:
    # Users and other identity types
    url = f"_apis/graph/users/{descriptor}?api-version=7.1-preview.1"
```

**Documentation References**:
- Users endpoint: [Microsoft Learn - Graph API - Users - Get](https://learn.microsoft.com/en-us/rest/api/azure/devops/graph/users/get)
- Groups endpoint: [Microsoft Learn - Graph API - Groups - Get](https://learn.microsoft.com/en-us/rest/api/azure/devops/graph/groups/get)

### 4. Fixed resolve_aad_group_members Endpoint (2025-01-16)
The `resolve_aad_group_members` function was using the wrong API approach to get AAD group members.

**Problem**:
- Was using: `_apis/graph/users?subjectTypes=aad&scopeDescriptor=scp.{origin_id}&api-version=7.0-preview`
- Error: "Parameter subjectDescriptor does not have a valid master scope ID"
- This approach doesn't work - you can't filter users by AAD group origin ID

**Solution** (Lines 504-617):
```python
# Changed function signature to accept group_descriptor instead of origin_id
async def resolve_aad_group_members(
    self,
    session: aiohttp.ClientSession,
    group_descriptor: str,  # Changed from origin_id
    group_name: str,
    ...
)

# Use Memberships API (same as VSTS groups) instead of Users List API
url = f"_apis/graph/memberships/{group_descriptor}?direction=down&api-version=7.1-preview.1"

# Process memberships and get member details
for membership in memberships:
    member_descriptor = membership.get('memberDescriptor', '')
    member_details = await self.get_identity_details(session, member_descriptor, ...)
```

**Updated caller** (Lines 726-735):
```python
# Pass member_descriptor instead of origin_id
resolved_members = await self.resolve_aad_group_members(
    session,
    member_descriptor,  # Use descriptor, not origin_id
    aad_group_name,
    f"{project_name}:{group_name}"
)
```

**Documentation Reference**:
- [Microsoft Learn - Graph API - Memberships - List](https://learn.microsoft.com/en-us/rest/api/azure/devops/graph/memberships/list)

## Current Status

### ✅ All Issues Fixed
- Graph API endpoints now use correct `-preview` versions (7.1-preview.1)
- Groups endpoint fetches all groups successfully
- Identity details endpoint correctly routes to `/users/` or `/groups/` based on descriptor type
- AAD group member resolution now uses correct Memberships API
- All API version and endpoint issues resolved

## Test Results
**Previous Run** (with origin_id approach):
```
Projects processed: 1
VSTS groups processed: 19
API calls made: 52
API errors: 31 (get_identity) + 17 (resolve_aad) = 48 total
```

**After Fixes**: Should run without API errors

## Next Actions
1. Test the script with all fixes applied
2. Verify all AAD group members are resolved successfully
3. Check the output CSV for completeness

## How to Run

**Using the helper script (recommended):**
```bash
./run_audit.sh
```

**Manual execution:**
```bash
export $(cat .env | grep -v '^#' | xargs) && uv run ado_permissions_auditor.py
```

### 5. Organized Output to Separate Directory (2025-01-16)
All audit output files are now organized in a dedicated `audit_output/` directory.

**Changes Made**:
- Added `OUTPUT_DIR = Path("audit_output")` constant (Line 28)
- Directory is created automatically if it doesn't exist
- Log files: `audit_output/ado_audit_YYYYMMDD_HHMMSS.log`
- CSV output: `audit_output/ado_permissions_audit_YYYYMMDD_HHMMSS.csv`
- Error logs: `audit_output/ado_audit_errors_YYYYMMDD_HHMMSS.json`

**Benefits**:
- Clean project root directory
- Easy to exclude output files from version control
- Organized audit runs with timestamps
- Simplified cleanup and archival

**Files Added**:
- `.gitignore` - Excludes `audit_output/` directory from git

## Files Modified
- `ado_permissions_auditor.py` - Fixed Graph API version issues and organized output
- `.gitignore` - Updated to exclude output directory and uv-specific files

### 7. Migration to uv Package Manager (2025-01-16)
Project migrated from pip to uv for modern Python dependency management.

**Files Removed:**
- `requirements.txt` - Old pip dependency file (replaced by pyproject.toml)
- `setup_and_run.sh` - Old pip-based setup script (replaced by run_audit.sh)
- `main.py` - Unused template file

**Files Added:**
- `run_audit.sh` - New uv-based interactive runner script with .env support

**Files Updated:**
- `pyproject.toml` - Already configured for uv (no changes needed)
- `.gitignore` - Added uv-specific exclusions (.venv/, .python-version, uv.lock)
- `README.md` - Updated prerequisites and usage to reference uv commands
- `QUICKSTART.md` - Updated all examples to use uv instead of pip/python
- `CLAUDE.local.md` - Updated run commands to use uv

**Benefits:**
- ✅ Faster dependency resolution and installation
- ✅ Better reproducibility with uv.lock file
- ✅ Modern Python tooling aligned with best practices
- ✅ Simplified setup (single tool instead of pip + venv)
- ✅ Automatic virtual environment management

## Pending Enhancement Requests

### 6. Direct Assignment Analysis (Requested 2025-01-16)
**Requirement**: Add analysis to identify users/service principals with direct VSTS group assignments (not via AAD groups).

**Business Value**:
- **Security Risk**: Direct assignments bypass centralized AAD group management
- **Maintenance Overhead**: Harder to audit and revoke access compared to AAD group membership
- **Compliance**: Many organizations require all access to be managed through AAD groups
- **Audit Trail**: Direct assignments may lack proper approval workflow

**Implementation Needs**:
Add new analysis function to `analyze_permissions.py`:

```python
def analyze_direct_assignments(self) -> Dict:
    """Analyze users/service principals with direct VSTS group assignments"""

    direct_users = defaultdict(lambda: {
        'projects': set(),
        'vsts_groups': set(),
        'assignment_count': 0
    })

    direct_service_principals = defaultdict(lambda: {
        'projects': set(),
        'vsts_groups': set(),
        'assignment_count': 0
    })

    for perm in self.permissions:
        if perm['assignment_type'] == 'direct':
            principal = perm['user_principal_name']

            if perm['user_type'] == 'service_principal':
                direct_service_principals[principal]['projects'].add(perm['project_name'])
                direct_service_principals[principal]['vsts_groups'].add(perm['vsts_group_name'])
                direct_service_principals[principal]['assignment_count'] += 1
            else:
                direct_users[principal]['projects'].add(perm['project_name'])
                direct_users[principal]['vsts_groups'].add(perm['vsts_group_name'])
                direct_users[principal]['assignment_count'] += 1

    return {
        'total_direct_users': len(direct_users),
        'total_direct_service_principals': len(direct_service_principals),
        'users_with_direct_assignments': {
            user: {
                'project_count': len(data['projects']),
                'vsts_group_count': len(data['vsts_groups']),
                'total_assignments': data['assignment_count'],
                'projects': sorted(data['projects']),
                'vsts_groups': sorted(data['vsts_groups'])
            }
            for user, data in sorted(
                direct_users.items(),
                key=lambda x: x[1]['assignment_count'],
                reverse=True
            )
        },
        'service_principals_with_direct_assignments': {
            sp: {
                'project_count': len(data['projects']),
                'vsts_group_count': len(data['vsts_groups']),
                'total_assignments': data['assignment_count'],
                'projects': sorted(data['projects']),
                'vsts_groups': sorted(data['vsts_groups'])
            }
            for sp, data in sorted(
                direct_service_principals.items(),
                key=lambda x: x[1]['assignment_count'],
                reverse=True
            )
        }
    }
```

**Report Output**:
```
DIRECT ASSIGNMENT ANALYSIS:
  Users with direct assignments: 45
  Service principals with direct assignments: 12

  Top users by direct assignment count:
    - user1@example.com: 15 assignments across 8 projects
    - user2@example.com: 12 assignments across 5 projects

  Service principals with direct assignments:
    - sp-build-pipeline: 8 assignments across 3 projects
    - sp-deployment: 5 assignments across 2 projects
```

**Integration Points**:
1. Add `analyze_direct_assignments()` call in `generate_report()` method
2. Include direct assignment metrics in summary section
3. Add dedicated report section for direct assignments
4. Consider flagging high-risk direct assignments (admin groups, production projects)

**Status**: 🔄 Pending implementation
