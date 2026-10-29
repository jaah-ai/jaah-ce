"""The macOS Full Disk Access profile: a PPPC `.mobileconfig` an MDM installs to grant `jaah-anchor` FDA.

A Mac's launchd jobs run through `/Library/Jaah/bin/jaah-anchor` (the one Developer ID-signed binary at a path
that never changes), so one grant to it reaches every process under it and survives updates. An unmanaged Mac
grants it by hand (the Add node guide); an MDM-managed one installs this profile (a user cannot install a TCC
payload — only an MDM can). The code requirement pins the anchor to the operator's Apple Team ID
(`Settings.apple_team_id`), so the grant covers no other binary at that path. Served by
`GET /api/nodes/macos/full-disk-access.mobileconfig` (`app/routes/nodes/cloud.py`).
"""
from __future__ import annotations

import plistlib
import uuid

ANCHOR_PATH = "/Library/Jaah/bin/jaah-anchor"
PROFILE_IDENTIFIER = "ai.jaah.full-disk-access"
DISPLAY_NAME = "Jaah node — Full Disk Access"
FILENAME = "jaah-full-disk-access.mobileconfig"
MEDIA_TYPE = "application/x-apple-aspen-config"

# Each PayloadUUID is uuid5 of this namespace and `<payload identifier>/<team id>`: a re-download is byte-identical,
# so an MDM sees the same profile, and a new Team ID is a new one.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "jaah.ai")


def code_requirement(team_id: str) -> str:
    """`jaah-anchor` signed with a Developer ID certificate of `team_id` (`codesign -d -r-` of the anchor)."""
    return (
        'identifier "jaah-anchor" and anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] '
        "/* exists */ and certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and "
        f'certificate leaf[subject.OU] = "{team_id}"'
    )


def _payload_uuid(identifier: str, team_id: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{identifier}/{team_id}")).upper()


def render_full_disk_access_profile(team_id: str) -> str:
    """The `.mobileconfig` XML granting `jaah-anchor`, signed by `team_id`, Full Disk Access."""
    tcc_identifier = f"{PROFILE_IDENTIFIER}.tcc"
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": PROFILE_IDENTIFIER,
        "PayloadUUID": _payload_uuid(PROFILE_IDENTIFIER, team_id),
        "PayloadDisplayName": DISPLAY_NAME,
        "PayloadDescription": "Lets the Jaah node mount NodeLinks and read its files.",
        "PayloadOrganization": "Jaah",
        "PayloadScope": "System",
        "PayloadContent": [{
            "PayloadType": "com.apple.TCC.configuration-profile-policy",
            "PayloadVersion": 1,
            "PayloadIdentifier": tcc_identifier,
            "PayloadUUID": _payload_uuid(tcc_identifier, team_id),
            "PayloadDisplayName": DISPLAY_NAME,
            "Services": {
                "SystemPolicyAllFiles": [{
                    "Identifier": ANCHOR_PATH,
                    "IdentifierType": "path",
                    "CodeRequirement": code_requirement(team_id),
                    "Allowed": True,
                    "Comment": "Jaah node daemons run through this launcher.",
                }],
            },
        }],
    }
    return plistlib.dumps(profile, sort_keys=False).decode("utf-8")
