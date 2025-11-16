"""
Azure DevOps Permissions Auditor
Efficiently dumps all user and service principal permissions across all projects.

Key Features:
- Advanced multi-level caching (AAD groups, users, group memberships)
- Robust error handling with detailed logging
- Concurrent API calls with rate limit handling
- Handles complex nested AAD group structures
- Memory-efficient streaming to CSV
"""

import asyncio
import base64
import csv
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import aiohttp

# Create output directory for all audit files
OUTPUT_DIR = Path("audit_output")
OUTPUT_DIR.mkdir(exist_ok=True)

# Configure logging to output directory
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(OUTPUT_DIR / f"ado_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


class MemberType(Enum):
    """Types of members in Azure DevOps"""

    USER = "user"
    GROUP = "group"
    SERVICE_PRINCIPAL = "service_principal"
    AAD_GROUP = "aad_group"
    UNKNOWN = "unknown"


@dataclass
class PermissionEntry:
    """Represents a single permission assignment"""

    project_name: str
    project_id: str
    user_principal_name: str
    user_display_name: str
    user_id: str
    user_type: str
    vsts_group_name: str
    vsts_group_id: str
    assignment_type: str  # 'direct' or AAD group name
    aad_group_chain: str = ""  # Full chain if nested: "GroupA > GroupB > GroupC"

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for CSV writing"""
        return asdict(self)


@dataclass
class CacheStats:
    """Statistics for cache performance"""

    aad_group_hits: int = 0
    aad_group_misses: int = 0
    user_hits: int = 0
    user_misses: int = 0
    group_membership_hits: int = 0
    group_membership_misses: int = 0

    def hit_rate(self, hits: int, misses: int) -> float:
        """Calculate hit rate percentage"""
        total = hits + misses
        return (hits / total * 100) if total > 0 else 0.0


class AzureDevOpsAuditor:
    """Main auditor class for Azure DevOps permissions with advanced caching"""

    def __init__(self, organization: str, pat_token: str, max_concurrent: int = 30):
        """
        Initialize the auditor

        Args:
            organization: Azure DevOps organization name
            pat_token: Personal Access Token with read permissions
            max_concurrent: Maximum concurrent API requests (default: 30 to avoid throttling)

        """
        self.organization = organization
        self.base_url = f"https://dev.azure.com/{organization}"
        self.vsaex_url = f"https://vsaex.dev.azure.com/{organization}"
        self.vssps_url = f"https://vssps.dev.azure.com/{organization}"

        # Encode PAT token for basic auth
        auth_string = f":{pat_token}"
        self.auth_header = base64.b64encode(auth_string.encode()).decode()

        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Multi-level caching system
        # Cache 1: AAD group members (originId -> list of members with their details)
        self.aad_group_members_cache: dict[str, list[dict[str, Any]]] = {}

        # Cache 2: User/SP details by descriptor (descriptor -> user details)
        self.identity_cache: dict[str, dict[str, Any]] = {}

        # Cache 3: VSTS group memberships per project (project_id:group_descriptor -> list of members)
        self.vsts_group_membership_cache: dict[str, list[dict[str, Any]]] = {}

        # Cache 4: Project details (project_id -> project info)
        self.project_cache: dict[str, dict[str, Any]] = {}

        # Cache 5: All VSTS groups per project (project_id -> list of groups)
        self.project_groups_cache: dict[str, list[dict[str, Any]]] = {}

        # Track which AAD groups are currently being resolved to avoid duplicate concurrent requests
        self.resolving_aad_groups: set[str] = set()
        self.aad_group_locks: dict[str, asyncio.Lock] = {}

        # Statistics
        self.cache_stats = CacheStats()
        self.stats = {
            "projects_processed": 0,
            "vsts_groups_processed": 0,
            "aad_groups_resolved": 0,
            "total_permissions": 0,
            "api_calls": 0,
            "api_errors": 0,
            "rate_limit_hits": 0,
        }

        # Error tracking
        self.errors: list[dict[str, Any]] = []

    def _get_aad_lock(self, group_id: str) -> asyncio.Lock:
        """Get or create a lock for an AAD group to prevent duplicate concurrent resolutions"""
        if group_id not in self.aad_group_locks:
            self.aad_group_locks[group_id] = asyncio.Lock()
        return self.aad_group_locks[group_id]

    async def _make_request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        method: str = "GET",
        base_url: str | None = None,
        params: dict | None = None,
        context: str = "",
    ) -> dict[str, Any] | None:
        """
        Make an authenticated API request with comprehensive error handling

        Args:
            session: aiohttp session
            url: API endpoint (relative or absolute)
            method: HTTP method
            base_url: Override base URL if needed
            params: Query parameters
            context: Context string for logging

        Returns:
            JSON response or None on error

        """
        async with self.semaphore:
            # Build full URL
            if not url.startswith("http"):
                base = base_url or self.base_url
                url = f"{base}/{url}"

            headers = {
                "Authorization": f"Basic {self.auth_header}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }

            retry_count = 0
            max_retries = 3
            backoff_base = 2  # Exponential backoff

            while retry_count <= max_retries:
                try:
                    self.stats["api_calls"] += 1

                    async with session.request(
                        method, url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        # Handle rate limiting
                        if response.status == 429:
                            self.stats["rate_limit_hits"] += 1
                            retry_after = int(response.headers.get("Retry-After", 60))
                            logger.warning(
                                f"Rate limited ({context}). Waiting {retry_after}s... "
                                f"[Retry {retry_count + 1}/{max_retries}]"
                            )
                            await asyncio.sleep(retry_after)
                            retry_count += 1
                            continue

                        # Handle not found (often expected for optional resources)
                        if response.status == 404:
                            logger.debug(f"Resource not found: {url} ({context})")
                            return None

                        # Handle unauthorized/forbidden
                        if response.status in (401, 403):
                            error_text = await response.text()
                            logger.error(
                                f"Authorization failed: {url} ({context}) - Status: {response.status} - {error_text}"
                            )
                            self.stats["api_errors"] += 1
                            self._log_error("auth_error", url, context, response.status, error_text)
                            return None

                        # Handle other client errors
                        if 400 <= response.status < 500:
                            error_text = await response.text()
                            logger.warning(
                                f"Client error: {url} ({context}) - Status: {response.status} - {error_text}"
                            )
                            self.stats["api_errors"] += 1
                            self._log_error("client_error", url, context, response.status, error_text)
                            return None

                        # Handle server errors with retry
                        if response.status >= 500:
                            error_text = await response.text()
                            if retry_count < max_retries:
                                wait_time = backoff_base**retry_count
                                logger.warning(
                                    f"Server error: {url} ({context}) - "
                                    f"Status: {response.status}. Retrying in {wait_time}s... "
                                    f"[Retry {retry_count + 1}/{max_retries}]"
                                )
                                await asyncio.sleep(wait_time)
                                retry_count += 1
                                continue
                            logger.error(
                                f"Server error after {max_retries} retries: {url} ({context}) - "
                                f"Status: {response.status} - {error_text}"
                            )
                            self.stats["api_errors"] += 1
                            self._log_error("server_error", url, context, response.status, error_text)
                            return None

                        # Success - parse JSON
                        try:
                            return await response.json()
                        except json.JSONDecodeError as e:
                            text = await response.text()
                            logger.error(f"JSON decode error: {url} ({context}) - {e} - Response: {text[:200]}")
                            self.stats["api_errors"] += 1
                            self._log_error("json_error", url, context, response.status, str(e))
                            return None

                except TimeoutError:
                    if retry_count < max_retries:
                        wait_time = backoff_base**retry_count
                        logger.warning(
                            f"Request timeout: {url} ({context}). "
                            f"Retrying in {wait_time}s... [Retry {retry_count + 1}/{max_retries}]"
                        )
                        await asyncio.sleep(wait_time)
                        retry_count += 1
                        continue
                    logger.error(f"Request timeout after {max_retries} retries: {url} ({context})")
                    self.stats["api_errors"] += 1
                    self._log_error("timeout", url, context, 0, "Request timeout")
                    return None

                except aiohttp.ClientError as e:
                    if retry_count < max_retries:
                        wait_time = backoff_base**retry_count
                        logger.warning(
                            f"Client error: {url} ({context}) - {e}. "
                            f"Retrying in {wait_time}s... [Retry {retry_count + 1}/{max_retries}]"
                        )
                        await asyncio.sleep(wait_time)
                        retry_count += 1
                        continue
                    logger.error(f"Client error after {max_retries} retries: {url} ({context}) - {e}")
                    self.stats["api_errors"] += 1
                    self._log_error("client_exception", url, context, 0, str(e))
                    return None

                except Exception as e:
                    logger.error(f"Unexpected error: {url} ({context}) - {type(e).__name__}: {e}")
                    self.stats["api_errors"] += 1
                    self._log_error("unexpected_error", url, context, 0, str(e))
                    return None

            return None

    def _log_error(self, error_type: str, url: str, context: str, status: int, message: str) -> None:
        """Log an error for later review"""
        self.errors.append(
            {
                "timestamp": datetime.now().isoformat(),
                "error_type": error_type,
                "url": url,
                "context": context,
                "status": status,
                "message": message,
            }
        )

    async def get_all_projects(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        """
        Get all projects in the organization

        Returns:
            List of project dictionaries

        """
        logger.info("Fetching all projects...")

        url = "_apis/projects?api-version=7.0"
        response = await self._make_request(session, url, context="get_all_projects")

        if not response or "value" not in response:
            logger.error("Failed to fetch projects or no projects found")
            return []

        projects = response["value"]

        # Cache projects
        for project in projects:
            self.project_cache[project["id"]] = project

        logger.info(f"Found {len(projects)} projects")
        return projects

    async def get_project_groups(
        self, session: aiohttp.ClientSession, project_id: str, project_name: str
    ) -> list[dict[str, Any]]:
        """
        Get all security groups for a project with caching

        Args:
            session: aiohttp session
            project_id: Project ID
            project_name: Project name for logging

        Returns:
            List of group dictionaries

        """
        # Check cache first
        if project_id in self.project_groups_cache:
            self.cache_stats.group_membership_hits += 1
            logger.debug(f"Cache hit: Groups for project {project_name}")
            return self.project_groups_cache[project_id]

        self.cache_stats.group_membership_misses += 1

        # Note: scopeDescriptor parameter removed - we'll filter by project after fetching
        url = "_apis/graph/groups?api-version=7.0-preview"
        response = await self._make_request(session, url, base_url=self.vssps_url, context=f"get_groups:{project_name}")

        if not response or "value" not in response:
            logger.warning(f"No groups found for project {project_name}")
            return []

        groups = response["value"]

        # Cache the groups
        self.project_groups_cache[project_id] = groups

        logger.debug(f"Found {len(groups)} groups in project {project_name}")
        return groups

    async def get_group_members(
        self, session: aiohttp.ClientSession, group_descriptor: str, group_name: str, project_name: str
    ) -> list[dict[str, Any]]:
        """
        Get direct members of a VSTS group with caching

        Args:
            session: aiohttp session
            group_descriptor: Group descriptor
            group_name: Group name for logging
            project_name: Project name for logging

        Returns:
            List of member dictionaries

        """
        cache_key = f"{project_name}:{group_descriptor}"

        # Check cache first
        if cache_key in self.vsts_group_membership_cache:
            self.cache_stats.group_membership_hits += 1
            logger.debug(f"Cache hit: Members for {group_name} in {project_name}")
            return self.vsts_group_membership_cache[cache_key]

        self.cache_stats.group_membership_misses += 1

        url = f"_apis/graph/memberships/{group_descriptor}?direction=down&api-version=7.0-preview"
        response = await self._make_request(
            session, url, base_url=self.vssps_url, context=f"get_members:{project_name}:{group_name}"
        )

        if not response or "value" not in response:
            logger.debug(f"No members found for group {group_name} in {project_name}")
            self.vsts_group_membership_cache[cache_key] = []
            return []

        members = response["value"]

        # Cache the members
        self.vsts_group_membership_cache[cache_key] = members

        logger.debug(f"Found {len(members)} direct members in group {group_name} (project: {project_name})")
        return members

    async def get_identity_details(
        self, session: aiohttp.ClientSession, descriptor: str, context_info: str = ""
    ) -> dict[str, Any] | None:
        """
        Get detailed information about an identity (user, group, or service principal)
        with caching

        Args:
            session: aiohttp session
            descriptor: Identity descriptor (from memberDescriptor field)
            context_info: Additional context for logging

        Returns:
            Identity details or None

        """
        # Check cache first
        if descriptor in self.identity_cache:
            self.cache_stats.user_hits += 1
            return self.identity_cache[descriptor]

        self.cache_stats.user_misses += 1

        # Different descriptor types require different endpoints
        # Group descriptors (vssgp, aadgp) -> /groups/ endpoint
        # User descriptors (aad, etc.) -> /users/ endpoint
        if descriptor.startswith(("vssgp.", "aadgp.")):
            # VSTS groups and AAD groups
            url = f"_apis/graph/groups/{descriptor}?api-version=7.1-preview.1"
        else:
            # Users and other identity types
            url = f"_apis/graph/users/{descriptor}?api-version=7.1-preview.1"

        response = await self._make_request(
            session, url, base_url=self.vssps_url, context=f"get_identity:{context_info}"
        )

        if not response:
            logger.debug(f"Could not fetch identity details for {descriptor} ({context_info})")
            return None

        # Cache the identity
        self.identity_cache[descriptor] = response

        return response

    async def resolve_aad_group_members(
        self,
        session: aiohttp.ClientSession,
        group_descriptor: str,
        group_name: str,
        context_info: str = "",
        chain: list[str] = None,
    ) -> list[tuple[dict[str, Any], str]]:
        """
        Recursively resolve AAD group membership with advanced caching and cycle detection

        Args:
            session: aiohttp session
            group_descriptor: AAD group descriptor (e.g., aadgp.*)
            group_name: Group name for logging
            context_info: Additional context
            chain: Current resolution chain for cycle detection

        Returns:
            List of tuples: (member_details, full_chain_string)

        """
        if chain is None:
            chain = []

        # Cycle detection
        if group_descriptor in chain:
            logger.warning(f"Cycle detected in AAD group resolution: {' -> '.join(chain)} -> {group_descriptor}")
            return []

        # Check cache first
        if group_descriptor in self.aad_group_members_cache:
            self.cache_stats.aad_group_hits += 1
            logger.debug(f"Cache hit: AAD group {group_name} ({group_descriptor})")
            cached_members = self.aad_group_members_cache[group_descriptor]

            # Return cached members with updated chain
            new_chain = chain + [group_name]
            chain_str = " > ".join(new_chain)
            return [(member, chain_str) for member in cached_members]

        self.cache_stats.aad_group_misses += 1

        # Use lock to prevent duplicate concurrent resolutions
        lock = self._get_aad_lock(group_descriptor)
        async with lock:
            # Double-check cache after acquiring lock
            if group_descriptor in self.aad_group_members_cache:
                self.cache_stats.aad_group_hits += 1
                cached_members = self.aad_group_members_cache[group_descriptor]
                new_chain = chain + [group_name]
                chain_str = " > ".join(new_chain)
                return [(member, chain_str) for member in cached_members]

            logger.debug(f"Resolving AAD group: {group_name} ({group_descriptor}) - Chain: {chain}")

            # Fetch group members using Memberships API (same as VSTS groups)
            url = f"_apis/graph/memberships/{group_descriptor}?direction=down&api-version=7.1-preview.1"
            response = await self._make_request(
                session, url, base_url=self.vssps_url, context=f"resolve_aad:{group_name}:{context_info}"
            )

            if not response or "value" not in response:
                logger.debug(f"No members resolved for AAD group {group_name}")
                self.aad_group_members_cache[group_descriptor] = []
                return []

            memberships = response["value"]
            all_resolved_members = []

            # Process each membership to get member details
            for membership in memberships:
                member_descriptor = membership.get("memberDescriptor", "")

                if not member_descriptor:
                    continue

                # Get detailed identity information for the member
                member_details = await self.get_identity_details(session, member_descriptor, f"{group_name}:member")

                if not member_details:
                    logger.debug(f"Could not resolve member {member_descriptor} in AAD group {group_name}")
                    continue

                member_subject_kind = member_details.get("subjectKind", "")

                # If member is another AAD group, resolve recursively
                if member_subject_kind == "group":
                    member_display_name = member_details.get("displayName", "Unknown Group")

                    # Recursive resolution with updated chain
                    nested_members = await self.resolve_aad_group_members(
                        session, member_descriptor, member_display_name, context_info, chain + [group_name]
                    )
                    all_resolved_members.extend(nested_members)
                else:
                    # It's a user or service principal - add to results
                    all_resolved_members.append(member_details)

            # Cache the fully resolved members (only leaf users/SPs, not groups)
            leaf_members = [m for m in all_resolved_members if isinstance(m, dict)]
            self.aad_group_members_cache[group_descriptor] = leaf_members

            self.stats["aad_groups_resolved"] += 1
            logger.debug(
                f"Resolved AAD group {group_name}: {len(leaf_members)} leaf members "
                f"(total including nested: {len(all_resolved_members)})"
            )

            # Return with chain information
            new_chain = chain + [group_name]
            chain_str = " > ".join(new_chain)
            return [(member, chain_str) for member in leaf_members]

    def _determine_member_type(self, member: dict[str, Any]) -> MemberType:
        """Determine the type of member based on available attributes"""
        subject_kind = member.get("subjectKind", "").lower()
        domain = member.get("domain", "").lower()
        principal_name = member.get("principalName", "").lower()

        if subject_kind == "group":
            return MemberType.AAD_GROUP
        if subject_kind == "user":
            # Distinguish between service principals and real users
            if "app@" in principal_name or domain == "build":
                return MemberType.SERVICE_PRINCIPAL
            return MemberType.USER
        if "serviceaccount" in domain or "build" in domain:
            return MemberType.SERVICE_PRINCIPAL

        return MemberType.UNKNOWN

    async def process_project_permissions(
        self, session: aiohttp.ClientSession, project: dict[str, Any], csv_writer, csv_file
    ) -> None:
        """
        Process all permissions for a single project and write to CSV

        Args:
            session: aiohttp session
            project: Project dictionary
            csv_writer: CSV writer object
            csv_file: File handle for flushing

        """
        project_id = project["id"]
        project_name = project["name"]

        try:
            logger.info(f"Processing project: {project_name}")

            # Get all groups in the project
            groups = await self.get_project_groups(session, project_id, project_name)

            if not groups:
                logger.warning(f"No groups found in project {project_name}")
                return

            # Process each group
            for group in groups:
                try:
                    group_descriptor = group.get("descriptor")
                    group_name = group.get("displayName", "Unknown Group")

                    if not group_descriptor:
                        logger.warning(f"Group {group_name} in {project_name} has no descriptor, skipping")
                        continue

                    logger.debug(f"Processing group: {group_name} in {project_name}")

                    # Get direct members of the group
                    memberships = await self.get_group_members(session, group_descriptor, group_name, project_name)

                    self.stats["vsts_groups_processed"] += 1

                    # Process each membership
                    for membership in memberships:
                        try:
                            member_descriptor = membership.get("memberDescriptor")

                            if not member_descriptor:
                                continue

                            # Get detailed identity information
                            member_details = await self.get_identity_details(
                                session, member_descriptor, f"{project_name}:{group_name}"
                            )

                            if not member_details:
                                logger.debug(
                                    f"Could not resolve member {member_descriptor} in {group_name} ({project_name})"
                                )
                                continue

                            subject_kind = member_details.get("subjectKind", "")

                            # If it's an AAD group, resolve its members
                            if subject_kind == "group":
                                aad_group_name = member_details.get("displayName", "Unknown AAD Group")

                                # Resolve AAD group members recursively using the descriptor
                                resolved_members = await self.resolve_aad_group_members(
                                    session, member_descriptor, aad_group_name, f"{project_name}:{group_name}"
                                )

                                # Create permission entries for each resolved member
                                for resolved_member, chain in resolved_members:
                                    entry = self._create_permission_entry(
                                        project_name,
                                        project_id,
                                        resolved_member,
                                        group_name,
                                        group_descriptor,
                                        aad_group_name,
                                        chain,
                                    )

                                    if entry:
                                        csv_writer.writerow(entry.to_dict())
                                        self.stats["total_permissions"] += 1

                            else:
                                # Direct membership (user or service principal)
                                entry = self._create_permission_entry(
                                    project_name, project_id, member_details, group_name, group_descriptor, "direct", ""
                                )

                                if entry:
                                    csv_writer.writerow(entry.to_dict())
                                    self.stats["total_permissions"] += 1

                        except Exception as e:
                            logger.error(
                                f"Error processing membership in {group_name} ({project_name}): {e}", exc_info=True
                            )
                            self._log_error(
                                "membership_processing_error", "", f"{project_name}:{group_name}", 0, str(e)
                            )
                            continue

                except Exception as e:
                    logger.error(
                        f"Error processing group {group.get('displayName', 'Unknown')} in {project_name}: {e}",
                        exc_info=True,
                    )
                    self._log_error(
                        "group_processing_error", "", f"{project_name}:{group.get('displayName', 'Unknown')}", 0, str(e)
                    )
                    continue

            # Flush CSV after each project to avoid data loss
            csv_file.flush()

            self.stats["projects_processed"] += 1
            logger.info(f"Completed project {project_name} ({self.stats['projects_processed']} projects done)")

        except Exception as e:
            logger.error(f"Error processing project {project_name}: {e}", exc_info=True)
            self._log_error("project_processing_error", "", project_name, 0, str(e))

    def _create_permission_entry(
        self,
        project_name: str,
        project_id: str,
        member: dict[str, Any],
        vsts_group_name: str,
        vsts_group_id: str,
        assignment_type: str,
        aad_chain: str,
    ) -> PermissionEntry | None:
        """
        Create a permission entry from member details

        Args:
            project_name: Project name
            project_id: Project ID
            member: Member details dictionary
            vsts_group_name: VSTS group name
            vsts_group_id: VSTS group ID/descriptor
            assignment_type: 'direct' or AAD group name
            aad_chain: Full AAD group chain if nested

        Returns:
            PermissionEntry or None if member details are insufficient

        """
        try:
            principal_name = member.get("principalName", "")
            display_name = member.get("displayName", "")
            user_id = member.get("originId", member.get("descriptor", ""))

            # Determine member type
            member_type = self._determine_member_type(member)

            # Skip if essential info is missing
            if not principal_name and not display_name:
                logger.debug(f"Skipping member with no name: {member}")
                return None

            return PermissionEntry(
                project_name=project_name,
                project_id=project_id,
                user_principal_name=principal_name or display_name,
                user_display_name=display_name or principal_name,
                user_id=user_id,
                user_type=member_type.value,
                vsts_group_name=vsts_group_name,
                vsts_group_id=vsts_group_id,
                assignment_type=assignment_type,
                aad_group_chain=aad_chain,
            )

        except Exception as e:
            logger.error(f"Error creating permission entry: {e}", exc_info=True)
            return None

    async def run_audit(self, output_file: str = "ado_permissions_audit.csv") -> None:
        """
        Run the complete audit process

        Args:
            output_file: Output CSV file path

        """
        start_time = datetime.now()
        logger.info(f"Starting Azure DevOps permissions audit at {start_time}")
        logger.info(f"Organization: {self.organization}")
        logger.info(f"Max concurrent requests: {self.max_concurrent}")
        logger.info(f"Output file: {output_file}")

        try:
            # Create aiohttp session with connection pooling
            connector = aiohttp.TCPConnector(limit=self.max_concurrent, limit_per_host=self.max_concurrent)
            timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=30)

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # Fetch all projects
                projects = await self.get_all_projects(session)

                if not projects:
                    logger.error("No projects found or failed to fetch projects. Exiting.")
                    return

                # Open CSV file for writing
                with open(output_file, "w", newline="", encoding="utf-8") as csv_file:
                    fieldnames = [
                        "project_name",
                        "project_id",
                        "user_principal_name",
                        "user_display_name",
                        "user_id",
                        "user_type",
                        "vsts_group_name",
                        "vsts_group_id",
                        "assignment_type",
                        "aad_group_chain",
                    ]

                    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                    writer.writeheader()
                    csv_file.flush()

                    # Process projects with controlled concurrency
                    # Process in batches to manage memory and rate limits
                    batch_size = 10

                    for i in range(0, len(projects), batch_size):
                        batch = projects[i : i + batch_size]
                        batch_num = i // batch_size + 1
                        total_batches = (len(projects) + batch_size - 1) // batch_size

                        logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} projects)")

                        # Process batch concurrently
                        tasks = [
                            self.process_project_permissions(session, project, writer, csv_file) for project in batch
                        ]

                        await asyncio.gather(*tasks, return_exceptions=True)

                        # Log progress
                        self._log_progress(start_time)

                        # Small delay between batches to be respectful to API
                        if i + batch_size < len(projects):
                            await asyncio.sleep(2)

            # Final statistics
            end_time = datetime.now()
            duration = end_time - start_time

            self._log_final_stats(start_time, end_time, duration, output_file)

        except Exception as e:
            logger.error(f"Fatal error during audit: {e}", exc_info=True)
            self._log_error("fatal_error", "", "audit", 0, str(e))
            raise

    def _log_progress(self, start_time: datetime) -> None:
        """Log current progress statistics"""
        elapsed = (datetime.now() - start_time).total_seconds()

        logger.info("=" * 80)
        logger.info("PROGRESS UPDATE")
        logger.info(f"  Projects processed: {self.stats['projects_processed']}")
        logger.info(f"  VSTS groups processed: {self.stats['vsts_groups_processed']}")
        logger.info(f"  AAD groups resolved: {self.stats['aad_groups_resolved']}")
        logger.info(f"  Total permissions found: {self.stats['total_permissions']}")
        logger.info(f"  API calls made: {self.stats['api_calls']}")
        logger.info(f"  API errors: {self.stats['api_errors']}")
        logger.info(f"  Rate limit hits: {self.stats['rate_limit_hits']}")
        logger.info(f"  Elapsed time: {elapsed:.1f}s")

        # Cache statistics
        aad_hit_rate = self.cache_stats.hit_rate(self.cache_stats.aad_group_hits, self.cache_stats.aad_group_misses)
        user_hit_rate = self.cache_stats.hit_rate(self.cache_stats.user_hits, self.cache_stats.user_misses)
        group_hit_rate = self.cache_stats.hit_rate(
            self.cache_stats.group_membership_hits, self.cache_stats.group_membership_misses
        )

        logger.info("CACHE PERFORMANCE:")
        logger.info(
            f"  AAD groups: {aad_hit_rate:.1f}% hit rate "
            f"({self.cache_stats.aad_group_hits} hits, "
            f"{self.cache_stats.aad_group_misses} misses)"
        )
        logger.info(
            f"  Identities: {user_hit_rate:.1f}% hit rate "
            f"({self.cache_stats.user_hits} hits, "
            f"{self.cache_stats.user_misses} misses)"
        )
        logger.info(
            f"  Group memberships: {group_hit_rate:.1f}% hit rate "
            f"({self.cache_stats.group_membership_hits} hits, "
            f"{self.cache_stats.group_membership_misses} misses)"
        )
        logger.info("=" * 80)

    def _log_final_stats(self, start_time: datetime, end_time: datetime, duration, output_file: str) -> None:
        """Log final audit statistics"""
        logger.info("\n" + "=" * 80)
        logger.info("AUDIT COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        logger.info(f"Start time: {start_time}")
        logger.info(f"End time: {end_time}")
        logger.info(f"Total duration: {duration}")
        logger.info(f"Output file: {output_file}")
        logger.info("")
        logger.info("FINAL STATISTICS:")
        logger.info(f"  Projects processed: {self.stats['projects_processed']}")
        logger.info(f"  VSTS groups processed: {self.stats['vsts_groups_processed']}")
        logger.info(f"  AAD groups resolved: {self.stats['aad_groups_resolved']}")
        logger.info(f"  Total permissions exported: {self.stats['total_permissions']}")
        logger.info(f"  Total API calls: {self.stats['api_calls']}")
        logger.info(f"  API errors encountered: {self.stats['api_errors']}")
        logger.info(f"  Rate limit hits: {self.stats['rate_limit_hits']}")
        logger.info("")

        # Cache efficiency
        total_aad = self.cache_stats.aad_group_hits + self.cache_stats.aad_group_misses
        total_users = self.cache_stats.user_hits + self.cache_stats.user_misses
        total_groups = self.cache_stats.group_membership_hits + self.cache_stats.group_membership_misses

        aad_hit_rate = self.cache_stats.hit_rate(self.cache_stats.aad_group_hits, self.cache_stats.aad_group_misses)
        user_hit_rate = self.cache_stats.hit_rate(self.cache_stats.user_hits, self.cache_stats.user_misses)
        group_hit_rate = self.cache_stats.hit_rate(
            self.cache_stats.group_membership_hits, self.cache_stats.group_membership_misses
        )

        logger.info("CACHE EFFICIENCY:")
        logger.info(f"  AAD groups: {aad_hit_rate:.1f}% (saved {self.cache_stats.aad_group_hits} API calls)")
        logger.info(f"  Identities: {user_hit_rate:.1f}% (saved {self.cache_stats.user_hits} API calls)")
        logger.info(
            f"  Group memberships: {group_hit_rate:.1f}% (saved {self.cache_stats.group_membership_hits} API calls)"
        )

        api_calls_saved = (
            self.cache_stats.aad_group_hits + self.cache_stats.user_hits + self.cache_stats.group_membership_hits
        )
        potential_calls = self.stats["api_calls"] + api_calls_saved

        logger.info(f"  Total API calls saved by caching: {api_calls_saved}")
        logger.info(f"  Efficiency gain: {(api_calls_saved / potential_calls * 100):.1f}%")

        if self.stats["api_errors"] > 0:
            logger.warning(f"\n{self.stats['api_errors']} errors occurred during audit.")
            logger.warning("Check the log file for details.")

            # Save errors to separate file in output directory
            error_file = OUTPUT_DIR / f"ado_audit_errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(error_file, "w") as f:
                json.dump(self.errors, f, indent=2)
            logger.warning(f"Detailed errors saved to: {error_file}")

        logger.info("=" * 80)


async def main():
    """Main entry point"""
    import os

    # Configuration - replace with your values or use environment variables
    ORGANIZATION = os.getenv("ADO_ORGANIZATION", "your-org-name")
    PAT_TOKEN = os.getenv("ADO_PAT_TOKEN")

    if not PAT_TOKEN:
        logger.error("PAT token not provided. Set ADO_PAT_TOKEN environment variable.")
        sys.exit(1)

    if ORGANIZATION == "your-org-name":
        logger.error("Organization not configured. Set ADO_ORGANIZATION environment variable.")
        sys.exit(1)

    # Output file with timestamp in output directory
    output_file = OUTPUT_DIR / f"ado_permissions_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    # Create auditor and run
    auditor = AzureDevOpsAuditor(
        organization=ORGANIZATION,
        pat_token=PAT_TOKEN,
        max_concurrent=30,  # Adjust based on your rate limits
    )

    try:
        await auditor.run_audit(str(output_file))
    except KeyboardInterrupt:
        logger.warning("\nAudit interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Audit failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
