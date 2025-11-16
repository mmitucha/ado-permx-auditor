"""
Azure DevOps Permissions Analyzer

Analyzes the audit CSV output to generate useful reports:
- Users with access to multiple projects
- AAD groups used across projects
- Service principals inventory
- Direct vs AAD group assignments
- Potential over-privileged users
"""

import csv
import json
from collections import defaultdict, Counter
from typing import Dict, List, Set
import sys


class PermissionsAnalyzer:
    """Analyzer for Azure DevOps permissions audit data"""
    
    def __init__(self, csv_file: str):
        """Initialize analyzer with CSV file path"""
        self.csv_file = csv_file
        self.permissions: List[Dict] = []
        self.load_data()
    
    def load_data(self):
        """Load CSV data"""
        print(f"Loading data from {self.csv_file}...")
        
        try:
            with open(self.csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                self.permissions = list(reader)
            
            print(f"Loaded {len(self.permissions)} permission entries")
        except Exception as e:
            print(f"Error loading CSV: {e}")
            sys.exit(1)
    
    def analyze_user_access(self) -> Dict:
        """Analyze users and their project access"""
        user_projects = defaultdict(set)
        user_groups = defaultdict(lambda: defaultdict(set))
        
        for perm in self.permissions:
            if perm['user_type'] == 'user':
                user = perm['user_principal_name']
                project = perm['project_name']
                group = perm['vsts_group_name']
                
                user_projects[user].add(project)
                user_groups[user][project].add(group)
        
        # Find users with broad access
        multi_project_users = {
            user: len(projects) 
            for user, projects in user_projects.items() 
            if len(projects) > 10
        }
        
        # Find potential admins (users in multiple admin groups)
        admin_keywords = ['admin', 'administrator']
        potential_admins = defaultdict(int)
        
        for user, projects_groups in user_groups.items():
            for project, groups in projects_groups.items():
                for group in groups:
                    if any(kw in group.lower() for kw in admin_keywords):
                        potential_admins[user] += 1
        
        return {
            'total_users': len(user_projects),
            'multi_project_users': dict(sorted(
                multi_project_users.items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:20]),  # Top 20
            'potential_admins': dict(sorted(
                potential_admins.items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:20]),  # Top 20
            'avg_projects_per_user': sum(len(p) for p in user_projects.values()) / len(user_projects)
        }
    
    def analyze_aad_groups(self) -> Dict:
        """Analyze AAD group usage"""
        aad_group_usage = defaultdict(lambda: {'projects': set(), 'vsts_groups': set(), 'users': set()})
        
        for perm in self.permissions:
            if perm['assignment_type'] != 'direct':
                # Extract first AAD group from chain
                aad_group = perm['assignment_type']
                if ' > ' in perm['aad_group_chain']:
                    aad_group = perm['aad_group_chain'].split(' > ')[0]
                
                aad_group_usage[aad_group]['projects'].add(perm['project_name'])
                aad_group_usage[aad_group]['vsts_groups'].add(perm['vsts_group_name'])
                aad_group_usage[aad_group]['users'].add(perm['user_principal_name'])
        
        # Find most reused AAD groups
        reused_groups = {
            group: {
                'project_count': len(data['projects']),
                'vsts_group_count': len(data['vsts_groups']),
                'user_count': len(data['users'])
            }
            for group, data in aad_group_usage.items()
        }
        
        reused_groups = dict(sorted(
            reused_groups.items(),
            key=lambda x: x[1]['project_count'],
            reverse=True
        ))
        
        # Calculate cache efficiency indicator
        total_aad_assignments = sum(
            1 for p in self.permissions 
            if p['assignment_type'] != 'direct'
        )
        unique_aad_groups = len(aad_group_usage)
        
        return {
            'total_aad_groups': unique_aad_groups,
            'total_aad_assignments': total_aad_assignments,
            'avg_projects_per_aad_group': (
                sum(len(d['projects']) for d in aad_group_usage.values()) / unique_aad_groups
                if unique_aad_groups > 0 else 0
            ),
            'most_reused_groups': dict(list(reused_groups.items())[:15]),  # Top 15
            'cache_efficiency_indicator': (
                f"Each AAD group is reused {total_aad_assignments / unique_aad_groups:.1f}x on average"
                if unique_aad_groups > 0 else "N/A"
            )
        }
    
    def analyze_service_principals(self) -> Dict:
        """Analyze service principal permissions"""
        sp_access = defaultdict(lambda: {'projects': set(), 'groups': set()})
        
        for perm in self.permissions:
            if perm['user_type'] == 'service_principal':
                sp = perm['user_principal_name']
                sp_access[sp]['projects'].add(perm['project_name'])
                sp_access[sp]['groups'].add(perm['vsts_group_name'])
        
        sp_summary = {
            sp: {
                'project_count': len(data['projects']),
                'groups': list(data['groups'])
            }
            for sp, data in sp_access.items()
        }
        
        return {
            'total_service_principals': len(sp_access),
            'service_principals': sp_summary
        }
    
    def analyze_assignment_types(self) -> Dict:
        """Analyze direct vs AAD group assignments"""
        direct_count = sum(1 for p in self.permissions if p['assignment_type'] == 'direct')
        aad_count = len(self.permissions) - direct_count
        
        # Breakdown by VSTS group type
        vsts_group_breakdown = defaultdict(lambda: {'direct': 0, 'aad': 0})
        
        for perm in self.permissions:
            group = perm['vsts_group_name']
            if perm['assignment_type'] == 'direct':
                vsts_group_breakdown[group]['direct'] += 1
            else:
                vsts_group_breakdown[group]['aad'] += 1
        
        return {
            'total_permissions': len(self.permissions),
            'direct_assignments': direct_count,
            'aad_group_assignments': aad_count,
            'direct_percentage': (direct_count / len(self.permissions) * 100) if self.permissions else 0,
            'aad_percentage': (aad_count / len(self.permissions) * 100) if self.permissions else 0,
            'vsts_group_breakdown': dict(vsts_group_breakdown)
        }
    
    def analyze_nested_groups(self) -> Dict:
        """Analyze nested AAD group structures"""
        nested_structures = defaultdict(int)
        max_depth = 0
        deep_chains = []
        
        for perm in self.permissions:
            chain = perm.get('aad_group_chain', '')
            if chain and ' > ' in chain:
                depth = len(chain.split(' > '))
                nested_structures[depth] += 1
                
                if depth > max_depth:
                    max_depth = depth
                
                if depth >= 3:
                    deep_chains.append({
                        'chain': chain,
                        'user': perm['user_principal_name'],
                        'project': perm['project_name'],
                        'depth': depth
                    })
        
        return {
            'max_nesting_depth': max_depth,
            'nesting_depth_distribution': dict(nested_structures),
            'deeply_nested_examples': sorted(deep_chains, key=lambda x: x['depth'], reverse=True)[:10]
        }
    
    def analyze_projects(self) -> Dict:
        """Analyze project-level statistics"""
        project_stats = defaultdict(lambda: {
            'total_permissions': 0,
            'unique_users': set(),
            'unique_groups': set(),
            'direct_assignments': 0,
            'aad_assignments': 0
        })
        
        for perm in self.permissions:
            project = perm['project_name']
            project_stats[project]['total_permissions'] += 1
            project_stats[project]['unique_users'].add(perm['user_principal_name'])
            project_stats[project]['unique_groups'].add(perm['vsts_group_name'])
            
            if perm['assignment_type'] == 'direct':
                project_stats[project]['direct_assignments'] += 1
            else:
                project_stats[project]['aad_assignments'] += 1
        
        # Convert sets to counts
        project_summary = {}
        for project, stats in project_stats.items():
            project_summary[project] = {
                'total_permissions': stats['total_permissions'],
                'unique_users': len(stats['unique_users']),
                'unique_groups': len(stats['unique_groups']),
                'direct_assignments': stats['direct_assignments'],
                'aad_assignments': stats['aad_assignments']
            }
        
        # Top projects by permission count
        top_projects = dict(sorted(
            project_summary.items(),
            key=lambda x: x[1]['total_permissions'],
            reverse=True
        )[:15])
        
        return {
            'total_projects': len(project_stats),
            'avg_permissions_per_project': (
                sum(s['total_permissions'] for s in project_summary.values()) / len(project_summary)
                if project_summary else 0
            ),
            'avg_users_per_project': (
                sum(s['unique_users'] for s in project_summary.values()) / len(project_summary)
                if project_summary else 0
            ),
            'top_projects_by_permissions': top_projects
        }
    
    def generate_report(self, output_file: str = None):
        """Generate comprehensive analysis report"""
        print("\n" + "=" * 80)
        print("AZURE DEVOPS PERMISSIONS ANALYSIS REPORT")
        print("=" * 80)
        
        # Run all analyses
        user_analysis = self.analyze_user_access()
        aad_analysis = self.analyze_aad_groups()
        sp_analysis = self.analyze_service_principals()
        assignment_analysis = self.analyze_assignment_types()
        nested_analysis = self.analyze_nested_groups()
        project_analysis = self.analyze_projects()
        
        report = {
            'summary': {
                'total_permissions': len(self.permissions),
                'total_projects': project_analysis['total_projects'],
                'total_users': user_analysis['total_users'],
                'total_service_principals': sp_analysis['total_service_principals'],
                'total_aad_groups': aad_analysis['total_aad_groups']
            },
            'user_access': user_analysis,
            'aad_groups': aad_analysis,
            'service_principals': sp_analysis,
            'assignment_types': assignment_analysis,
            'nested_groups': nested_analysis,
            'projects': project_analysis
        }
        
        # Print summary
        print(f"\nTOTAL STATISTICS:")
        print(f"  Total permissions: {report['summary']['total_permissions']:,}")
        print(f"  Total projects: {report['summary']['total_projects']:,}")
        print(f"  Total users: {report['summary']['total_users']:,}")
        print(f"  Total service principals: {report['summary']['total_service_principals']:,}")
        print(f"  Total AAD groups: {report['summary']['total_aad_groups']:,}")
        
        print(f"\nASSIGNMENT BREAKDOWN:")
        print(f"  Direct assignments: {assignment_analysis['direct_assignments']:,} "
              f"({assignment_analysis['direct_percentage']:.1f}%)")
        print(f"  AAD group assignments: {assignment_analysis['aad_group_assignments']:,} "
              f"({assignment_analysis['aad_percentage']:.1f}%)")
        
        print(f"\nUSER ACCESS PATTERNS:")
        print(f"  Average projects per user: {user_analysis['avg_projects_per_user']:.1f}")
        print(f"  Users with 10+ projects: {len(user_analysis['multi_project_users'])}")
        
        if user_analysis['multi_project_users']:
            print(f"\n  Top users by project count:")
            for user, count in list(user_analysis['multi_project_users'].items())[:5]:
                print(f"    - {user}: {count} projects")
        
        print(f"\nAAD GROUP EFFICIENCY:")
        print(f"  {aad_analysis['cache_efficiency_indicator']}")
        print(f"  Average projects per AAD group: {aad_analysis['avg_projects_per_aad_group']:.1f}")
        
        if aad_analysis['most_reused_groups']:
            print(f"\n  Most reused AAD groups:")
            for group, stats in list(aad_analysis['most_reused_groups'].items())[:5]:
                print(f"    - {group}: {stats['project_count']} projects, "
                      f"{stats['user_count']} users")
        
        print(f"\nNESTED GROUP STRUCTURES:")
        print(f"  Maximum nesting depth: {nested_analysis['max_nesting_depth']}")
        
        if nested_analysis['deeply_nested_examples']:
            print(f"\n  Examples of deeply nested groups:")
            for example in nested_analysis['deeply_nested_examples'][:3]:
                print(f"    - Depth {example['depth']}: {example['chain']}")
        
        print(f"\nPROJECT STATISTICS:")
        print(f"  Average permissions per project: {project_analysis['avg_permissions_per_project']:.1f}")
        print(f"  Average users per project: {project_analysis['avg_users_per_project']:.1f}")
        
        # Save to JSON if output file specified
        if output_file:
            with open(output_file, 'w') as f:
                # Convert sets to lists for JSON serialization
                json.dump(report, f, indent=2, default=str)
            print(f"\nDetailed report saved to: {output_file}")
        
        print("=" * 80)
        
        return report


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python analyze_permissions.py <audit_csv_file> [output_json_file]")
        print("\nExample:")
        print("  python analyze_permissions.py ado_permissions_audit_20250101_120000.csv analysis_report.json")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    analyzer = PermissionsAnalyzer(csv_file)
    analyzer.generate_report(output_file)


if __name__ == '__main__':
    main()
