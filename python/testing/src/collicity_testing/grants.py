"""A beta-shaped Graph manifest and the inventory assignment's grants, for grant tests.

Modeled on the beta contract (spec §18.1): read the Stock table in Inventory.xlsx and change
Qty by at most 50 units, at most 20 rows a run; read the Requests folder; draft replies only to
the original sender.
"""

from decimal import Decimal
from typing import Any

from collicity_grants.models import (
    ActionKind,
    ActionSpec,
    AdminPolicy,
    CallRequest,
    CapabilityGrant,
    ConnectorManifest,
    CostModel,
    FieldLimit,
    FieldSpec,
    FieldType,
    Recovery,
)

CONNECTOR = "graph"
VERSION = "2026.10.1"
WORKBOOK, OTHER_WORKBOOK = "drive-item-inventory", "drive-item-payroll"
TABLE, OTHER_TABLE = "table-stock", "table-salaries"
FOLDER, OTHER_FOLDER = "folder-requests", "folder-inbox"
FREE = CostModel(free=True)

ROW_FIELDS = {
    "Item": FieldSpec(type=FieldType.TEXT),
    "Qty": FieldSpec(type=FieldType.NUMBER),
    "Status": FieldSpec(type=FieldType.CHOICE, choices=frozenset({"open", "ordered", "closed"})),
    "Note": FieldSpec(type=FieldType.TEXT),
    "Cost": FieldSpec(type=FieldType.NUMBER),
    "Due": FieldSpec(type=FieldType.DATE),
}
TABLE_RESOURCES = {"workbook": "drive_item", "table": "table"}


def manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector=CONNECTOR,
        version=VERSION,
        scopes=frozenset({"Files.Read", "Files.ReadWrite", "Mail.Read", "Mail.ReadWrite"}),
        hosts=frozenset({"graph.microsoft.com"}),
        actions={
            "excel.read_rows": ActionSpec(
                kind=ActionKind.READ,
                scopes=frozenset({"Files.Read"}),
                resource_params=TABLE_RESOURCES,
                other_params=frozenset({"top"}),
                fields=ROW_FIELDS,
                cost=FREE,
            ),
            "excel.update_row": ActionSpec(
                kind=ActionKind.WRITE,
                scopes=frozenset({"Files.ReadWrite"}),
                resource_params=TABLE_RESOURCES,
                write_param="values",
                other_params=frozenset({"row_id", "if_match"}),
                fields=ROW_FIELDS,
                duplicate_prevention="b",
                recovery=Recovery.COMPENSABLE,
                compensation="excel.update_row",
                cost=FREE,
            ),
            "excel.recalculate": ActionSpec(
                kind=ActionKind.READ,
                scopes=frozenset({"Files.Read"}),
                resource_params=TABLE_RESOURCES,
            ),
            "mail.list_messages": ActionSpec(
                kind=ActionKind.READ,
                scopes=frozenset({"Mail.Read"}),
                resource_params={"folder": "mail_folder"},
                other_params=frozenset({"top"}),
                fields={
                    "subject": FieldSpec(type=FieldType.TEXT),
                    "from": FieldSpec(type=FieldType.TEXT),
                    "body": FieldSpec(type=FieldType.TEXT),
                },
                cost=FREE,
            ),
            "mail.search": ActionSpec(
                kind=ActionKind.READ,
                scopes=frozenset({"Mail.Read"}),
                resource_params={"message": "message"},
                resource_determinable=False,
                other_params=frozenset({"query"}),
                cost=FREE,
            ),
            "mail.create_reply_draft": ActionSpec(
                kind=ActionKind.WRITE,
                scopes=frozenset({"Mail.ReadWrite"}),
                resource_params={"folder": "mail_folder"},
                recipient_params=("to", "cc", "bcc"),
                other_params=frozenset({"message_id", "body", "correlation_id"}),
                duplicate_prevention="c",
                recovery=Recovery.COMPENSABLE,
                compensation="mail.delete_draft",
                cost=FREE,
            ),
            "mail.delete_draft": ActionSpec(
                kind=ActionKind.DESTRUCTIVE,
                scopes=frozenset({"Mail.ReadWrite"}),
                resource_params={"folder": "mail_folder"},
                other_params=frozenset({"draft_id"}),
                duplicate_prevention="a",
                recovery=Recovery.IRREVERSIBLE,
                cost=FREE,
            ),
            "mail.send": ActionSpec(
                kind=ActionKind.WRITE,
                scopes=frozenset({"Mail.ReadWrite"}),
                resource_params={"folder": "mail_folder"},
                recipient_params=("to",),
                other_params=frozenset({"body"}),
                duplicate_prevention="d",
                recovery=Recovery.IRREVERSIBLE,
                publishes=True,
                cost=FREE,
            ),
            "sharepoint.grant_access": ActionSpec(
                kind=ActionKind.SECURITY,
                scopes=frozenset({"Files.ReadWrite"}),
                resource_params={"workbook": "drive_item"},
                other_params=frozenset({"principal", "role"}),
                duplicate_prevention="a",
                recovery=Recovery.IRREVERSIBLE,
                cost=FREE,
            ),
        },
    )


def grants() -> list[CapabilityGrant]:
    table = {"workbook": frozenset({WORKBOOK}), "table": frozenset({TABLE})}
    return [
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="excel.read_rows",
            resources=table,
            read_fields=frozenset({"Item", "Qty", "Status"}),
        ),
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="excel.update_row",
            resources=table,
            write_fields=frozenset({"Qty", "Status"}),
            limits={"Qty": FieldLimit(min=Decimal(0), max_change=Decimal(50))},
            max_records_per_run=20,
        ),
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="excel.recalculate",
            resources=table,
        ),
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="mail.list_messages",
            resources={"folder": frozenset({FOLDER})},
            read_fields=frozenset({"subject", "from", "body"}),
        ),
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="mail.create_reply_draft",
            resources={"folder": frozenset({FOLDER})},
            recipient_rule="original_sender_only",
        ),
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="mail.search",
        ),
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="sharepoint.grant_access",
            resources={"workbook": frozenset({WORKBOOK})},
        ),
        CapabilityGrant(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action="mail.send",
            resources={"folder": frozenset({FOLDER})},
            recipient_rule="original_sender_only",
        ),
    ]


def policy() -> AdminPolicy:
    return AdminPolicy(enabled_connectors=frozenset({CONNECTOR}))


def consented() -> frozenset[str]:
    return manifest().scopes


def request(action: str, **fields: Any) -> CallRequest:
    """A request for ``action`` with the granted table or folder resolved, plus ``fields``."""
    resources = (
        {"workbook": WORKBOOK, "table": TABLE}
        if action.startswith("excel.")
        else {"folder": FOLDER}
        if action.startswith("mail.") and action != "mail.search"
        else {}
    )
    base: dict[str, Any] = {
        "connector": CONNECTOR,
        "manifest_version": VERSION,
        "action": action,
        "resources": resources,
    }
    return CallRequest(**(base | fields))
