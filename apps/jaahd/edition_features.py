# GENERATED from packages/editions/features.toml by tools/editions/edition_map.py — do not edit.
"""The ce edition's features: guard a feature by its name, `edition_features.enabled("<slug>")`, read at call
time (never a from-import), so a test switches editions through `edition_fixture`. Import-free.
"""

LABELS: dict[str, str] = {
    'billing': 'Billing and plans',
    'hostings': 'Hostings',
    'cloud_nodes': 'Cloud nodes',
    'node_groups': 'Node groups',
    'placement_groups': 'Node groups',
    'multi_node_hosts': 'Multi-Node Hosts',
    'nodelink': 'NodeLink shared folders',
    'microvm': 'MicroVM sessions',
    'gha_runners': 'GitHub Actions runners',
    'apt_self_upgrade': 'Automatic node upgrades',
    'tailnet': 'Private network',
    'node_log_archive': 'Node log archive',
    'cpu_profiling': 'CPU profiling',
    'knowledge_base': 'Knowledge base',
    'support_assistant': 'Help assistant',
    'site_chat': 'Website chat',
    'project_memory': 'Project memory',
    'full_doctrine': 'Full agent doctrine',
    'compliance': 'Compliance',
    'merge_lane': 'Merge lane',
    'jaah_ai_credit': 'Jaah AI credit',
    'sso': 'Single sign-on',
    'scim': 'SCIM provisioning',
    'siem_export': 'SIEM export',
    'tenant_mail': 'Org email',
    'system_mail': 'System email',
    'data_residency': 'Data residency',
    'org_transfer': 'Org transfer',
    'multi_org_tenancy': 'Multiple orgs',
    'signup': 'Sign-up',
    'users_invites': 'Users and invitations',
    'oauth_signin': 'Sign in with GitHub, Google or Microsoft',
    'forge_connect': 'Connect with GitHub, GitLab, Bitbucket or Azure DevOps',
    'event_triggers': 'Event triggers',
    'slack': 'Slack',
    'telegram': 'Telegram',
    'legal_acceptance': 'Terms acceptance',
    'customers': 'Customers',
    'easy_mode_hosting': 'Easy Mode app hosting',
    'control_plane_view': 'Control plane view',
    'live_cast': 'Live Cast',
    'operator_dashboard': 'Operator dashboard',
    'model_prices': 'Model pricing',
    'trust_center': 'Trust center',
    'site_enquiry': 'Website enquiries',
    'session_efficiency': 'Session efficiency',
    'aws': 'Amazon Web Services',
    'exec_dashboard': 'Command center',
}
#: Each edition's features, the map's columns (`edition_fixture` switches `ENABLED` to one of them).
BY_EDITION: dict[str, frozenset[str]] = {
    'cloud': frozenset(LABELS),
    'ce': frozenset(),
}
#: Every feature in the map (`cloud` is on every row, so the columns' union is the whole map).
ALL: frozenset[str] = frozenset().union(*BY_EDITION.values())
ENABLED: frozenset[str] = BY_EDITION['ce']


def enabled(name: str) -> bool:
    """Whether this edition includes the feature `name`; a name the map does not hold is a KeyError."""
    if name not in ALL:
        raise KeyError(f"{name!r} is not a feature in packages/editions/features.toml")
    return name in ENABLED
