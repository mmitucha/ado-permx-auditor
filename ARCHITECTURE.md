# Azure DevOps Permissions Auditor - Architecture

## System Overview

The Azure DevOps Permissions Auditor is an asynchronous Python application that efficiently audits user and service principal permissions across all projects in an Azure DevOps organization.

## Architecture Diagram

```mermaid
graph TB
    subgraph "Entry Point"
        Main[main<br/>Entry Point]
    end

    subgraph "Core Auditor"
        Auditor[AzureDevOpsAuditor<br/>Main Orchestrator]
        Config[Configuration<br/>- Organization<br/>- PAT Token<br/>- Concurrency Limits]
    end

    subgraph "Multi-Level Cache System"
        Cache1[AAD Group Members Cache<br/>descriptor → members]
        Cache2[Identity Cache<br/>descriptor → details]
        Cache3[VSTS Group Membership Cache<br/>project:group → members]
        Cache4[Project Cache<br/>project_id → project info]
        Cache5[Project Groups Cache<br/>project_id → groups]
    end

    subgraph "API Layer"
        APIClient[_make_request<br/>HTTP Client with Retry Logic]
        Semaphore[Async Semaphore<br/>Rate Limiting]

        subgraph "API Endpoints"
            ProjectAPI[Projects API<br/>dev.azure.com]
            GraphAPI[Graph API<br/>vssps.dev.azure.com]
        end
    end

    subgraph "Core Processing Flow"
        GetProjects[get_all_projects<br/>Fetch All Projects]
        ProcessProject[process_project_permissions<br/>Process Single Project]
        GetGroups[get_project_groups<br/>Fetch VSTS Groups]
        GetMembers[get_group_members<br/>Fetch Group Members]
        GetIdentity[get_identity_details<br/>User/Group/SP Details]
        ResolveAAD[resolve_aad_group_members<br/>Recursive AAD Resolution]
    end

    subgraph "Data Models"
        PermEntry[PermissionEntry<br/>Output Data Model]
        MemberType[MemberType Enum<br/>USER/GROUP/SP/AAD_GROUP]
        CacheStats[CacheStats<br/>Performance Metrics]
    end

    subgraph "Output & Logging"
        CSVWriter[CSV Writer<br/>Streaming Output]
        Logger[Logging System<br/>- Progress Logs<br/>- Error Tracking]
        ErrorLog[Error Log JSON<br/>Detailed Error Info]
    end

    Main --> Config
    Config --> Auditor
    Auditor --> GetProjects
    GetProjects --> ProjectAPI
    GetProjects --> Cache4

    GetProjects --> ProcessProject
    ProcessProject --> GetGroups
    GetGroups --> GraphAPI
    GetGroups --> Cache5

    ProcessProject --> GetMembers
    GetMembers --> GraphAPI
    GetMembers --> Cache3

    GetMembers --> GetIdentity
    GetIdentity --> GraphAPI
    GetIdentity --> Cache2

    GetIdentity --> ResolveAAD
    ResolveAAD --> GraphAPI
    ResolveAAD --> Cache1
    ResolveAAD --> GetIdentity

    APIClient --> Semaphore
    APIClient --> ProjectAPI
    APIClient --> GraphAPI

    ProcessProject --> PermEntry
    PermEntry --> CSVWriter

    Auditor --> Logger
    Logger --> ErrorLog

    Cache1 -.-> CacheStats
    Cache2 -.-> CacheStats
    Cache3 -.-> CacheStats

    style Auditor fill:#4a9eff
    style Cache1 fill:#90ee90
    style Cache2 fill:#90ee90
    style Cache3 fill:#90ee90
    style Cache4 fill:#90ee90
    style Cache5 fill:#90ee90
    style APIClient fill:#ffb366
    style CSVWriter fill:#ffcc99
```

## Detailed Flow Diagram

```mermaid
sequenceDiagram
    participant Main
    participant Auditor
    participant Cache
    participant API as Azure DevOps API
    participant CSV

    Main->>Auditor: Initialize with config
    Auditor->>API: get_all_projects()
    API-->>Auditor: Projects list
    Auditor->>Cache: Cache projects

    loop For each project
        Auditor->>Cache: Check project_groups_cache
        alt Cache Hit
            Cache-->>Auditor: Cached groups
        else Cache Miss
            Auditor->>API: get_project_groups()
            API-->>Auditor: VSTS groups
            Auditor->>Cache: Store groups
        end

        loop For each VSTS group
            Auditor->>Cache: Check membership_cache
            alt Cache Hit
                Cache-->>Auditor: Cached members
            else Cache Miss
                Auditor->>API: get_group_members()
                API-->>Auditor: Member descriptors
                Auditor->>Cache: Store members
            end

            loop For each member
                Auditor->>Cache: Check identity_cache
                alt Cache Hit
                    Cache-->>Auditor: Cached identity
                else Cache Miss
                    Auditor->>API: get_identity_details()
                    API-->>Auditor: Identity details
                    Auditor->>Cache: Store identity
                end

                alt Member is AAD Group
                    Auditor->>Cache: Check aad_group_cache
                    alt Cache Hit
                        Cache-->>Auditor: Cached AAD members
                    else Cache Miss
                        Auditor->>API: resolve_aad_group_members()
                        API-->>Auditor: AAD members
                        Auditor->>Cache: Store AAD members
                    end

                    loop For each AAD member
                        Auditor->>CSV: Write permission entry
                    end
                else Member is User/SP
                    Auditor->>CSV: Write permission entry
                end
            end
        end
    end

    Auditor->>CSV: Flush and close
    Auditor->>Main: Return statistics
```

## Component Details

### Core Components

#### AzureDevOpsAuditor
**Responsibilities:**
- Orchestrate the entire audit process
- Manage API connections and rate limiting
- Coordinate caching strategy
- Handle error tracking and logging

**Key Methods:**
- `run_audit()`: Main entry point for audit execution
- `process_project_permissions()`: Process single project with concurrency
- `get_identity_details()`: Fetch and cache identity information
- `resolve_aad_group_members()`: Recursively resolve nested AAD groups

#### Multi-Level Cache System

**Cache Hierarchy:**
1. **AAD Group Members Cache**: Stores resolved AAD group members to avoid re-resolution
2. **Identity Cache**: Stores user/group/SP details by descriptor
3. **VSTS Group Membership Cache**: Stores group memberships per project
4. **Project Cache**: Stores project information
5. **Project Groups Cache**: Stores VSTS groups per project

**Benefits:**
- Reduces API calls by 60-80% (typical cache hit rate)
- Prevents duplicate concurrent resolutions with async locks
- Handles circular AAD group references with cycle detection

### API Integration

#### Endpoint Strategy

```mermaid
graph LR
    subgraph "API Routing Logic"
        Descriptor{Descriptor Type?}
        Descriptor -->|vssgp.* or aadgp.*| GroupsAPI[/groups/ endpoint]
        Descriptor -->|aad.* or other| UsersAPI[/users/ endpoint]

        GroupType{Need Members?}
        GroupType -->|Yes| MembershipsAPI[/memberships/ endpoint<br/>direction=down]
        GroupType -->|No| GroupsAPI
    end

    style GroupsAPI fill:#90ee90
    style UsersAPI fill:#90ee90
    style MembershipsAPI fill:#90ee90
```

**Key Endpoints:**
- **Projects**: `dev.azure.com/{org}/_apis/projects`
- **Groups**: `vssps.dev.azure.com/{org}/_apis/graph/groups/{descriptor}`
- **Users**: `vssps.dev.azure.com/{org}/_apis/graph/users/{descriptor}`
- **Memberships**: `vssps.dev.azure.com/{org}/_apis/graph/memberships/{descriptor}?direction=down`

#### Error Handling Strategy

```mermaid
graph TD
    Request[API Request]
    Request --> RateLimit{Status 429?}
    RateLimit -->|Yes| Wait[Wait Retry-After]
    Wait --> Retry{Retry < 3?}
    Retry -->|Yes| Request
    Retry -->|No| LogError[Log Rate Limit Error]

    RateLimit -->|No| ServerError{Status >= 500?}
    ServerError -->|Yes| Backoff[Exponential Backoff]
    Backoff --> Retry

    ServerError -->|No| ClientError{Status 4xx?}
    ClientError -->|Yes| LogClientError[Log Client Error]

    ClientError -->|No| Success[Parse JSON Response]
    Success --> Cache[Update Cache]

    style LogError fill:#ffcccc
    style LogClientError fill:#ffcccc
    style Success fill:#90ee90
    style Cache fill:#90ee90
```

### Data Flow

#### Permission Entry Creation

```mermaid
graph LR
    Member[Member Details] --> Determine[_determine_member_type]
    Determine --> Type{Member Type}

    Type -->|USER| UserEntry[Create User Entry]
    Type -->|SERVICE_PRINCIPAL| SPEntry[Create SP Entry]
    Type -->|GROUP| ResolveGroup[Resolve AAD Group]

    ResolveGroup --> NestedMembers[Get Nested Members]
    NestedMembers --> Chain[Build AAD Chain]
    Chain --> MultiEntry[Create Multiple Entries]

    UserEntry --> CSV[Write to CSV]
    SPEntry --> CSV
    MultiEntry --> CSV

    style CSV fill:#ffcc99
```

## Performance Characteristics

### Concurrency Model

```mermaid
graph TB
    subgraph "Concurrency Control"
        Semaphore[Async Semaphore<br/>Max: 30 concurrent]
        Batches[Project Batches<br/>Size: 10 projects]

        Semaphore --> Request1[API Request 1]
        Semaphore --> Request2[API Request 2]
        Semaphore --> RequestN[API Request N]

        Batches --> Batch1[Batch 1: Projects 1-10]
        Batches --> Batch2[Batch 2: Projects 11-20]

        Batch1 --> Process1[Concurrent Processing]
        Batch2 --> Process2[Concurrent Processing]
    end

    style Semaphore fill:#4a9eff
    style Batches fill:#4a9eff
```

**Key Metrics:**
- **Max Concurrent Requests**: 30 (configurable)
- **Project Batch Size**: 10 projects per batch
- **Rate Limit Handling**: Automatic retry with exponential backoff
- **Cache Hit Rate**: Typically 60-80%

### Optimization Strategies

1. **Async Locks for AAD Groups**: Prevent duplicate concurrent resolutions of the same group
2. **Cycle Detection**: Prevent infinite loops in circular AAD group references
3. **Streaming CSV Output**: Memory-efficient output with periodic flushing
4. **Batch Processing**: Process projects in batches to manage memory and rate limits

## Error Tracking

```mermaid
graph LR
    Error[Error Occurs] --> Log[Log to Logger]
    Log --> Track[Add to Error List]
    Track --> Continue[Continue Processing]

    Continue --> AuditEnd{Audit Complete?}
    AuditEnd -->|Yes| ErrorFile[Export errors.json]
    AuditEnd -->|No| Continue

    style Error fill:#ffcccc
    style ErrorFile fill:#ffcc99
```

**Error Categories:**
- `auth_error`: Authorization failures (401, 403)
- `client_error`: Client errors (400-499)
- `server_error`: Server errors (500+)
- `timeout`: Request timeouts
- `json_error`: JSON decode errors
- `unexpected_error`: Unhandled exceptions

## Output Format

### CSV Structure

| Field | Description |
|-------|-------------|
| `project_name` | Azure DevOps project name |
| `project_id` | Project GUID |
| `user_principal_name` | User/SP principal name |
| `user_display_name` | Display name |
| `user_id` | Origin ID or descriptor |
| `user_type` | USER/SERVICE_PRINCIPAL/GROUP/AAD_GROUP |
| `vsts_group_name` | VSTS security group name |
| `vsts_group_id` | VSTS group descriptor |
| `assignment_type` | 'direct' or group name |
| `assignment_group_type` | 'aad_group', 'vsts_group', or empty for direct |

### Example Output

```csv
project_name,project_id,user_principal_name,user_display_name,user_id,user_type,vsts_group_name,vsts_group_id,assignment_type,assignment_group_type
Main,abc-123,john@example.com,John Doe,xyz-789,user,Contributors,vssgp.XYZ,direct,
Main,abc-123,jane@example.com,Jane Smith,def-456,user,Readers,vssgp.ABC,DevTeam,aad_group
```

## Dependencies

- **aiohttp**: Async HTTP client for API requests
- **asyncio**: Async/await framework
- **csv**: CSV output writing
- **json**: JSON parsing and error export
- **base64**: PAT token encoding

## Configuration

### Environment Variables

- `ADO_ORGANIZATION`: Azure DevOps organization name
- `ADO_PAT_TOKEN`: Personal Access Token with read permissions

### Tunable Parameters

- `max_concurrent`: Maximum concurrent API requests (default: 30)
- `batch_size`: Projects per batch (default: 10)
- `max_retries`: API retry attempts (default: 3)
- `backoff_base`: Exponential backoff multiplier (default: 2)

## Key Design Decisions

### 1. Async Architecture
**Rationale**: Maximize throughput for I/O-bound API operations while respecting rate limits

### 2. Multi-Level Caching
**Rationale**: Minimize API calls and avoid duplicate work across projects with shared groups

### 3. Descriptor-Based Resolution
**Rationale**: Use graph descriptors instead of origin IDs for consistent API access across all entity types

### 4. Streaming CSV Output
**Rationale**: Memory-efficient output for large organizations with periodic flushing to prevent data loss

### 5. Recursive AAD Group Resolution
**Rationale**: Flatten nested AAD group structures to show actual user permissions with full chain visibility
