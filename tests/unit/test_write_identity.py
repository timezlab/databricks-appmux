"""Every registry write is routed to the identity of data model section 6.

Two identities write the registry: the appmux service principal, and a platform admin's own role
(a member of ``appmux_platform_writers``, reached with that person's OBO token). The table in
``appmux.registry.writers`` says which one each write kind uses. Three things are checked: the table
against the data model, a write with the other identity raises, and the table against the
``GRANT`` statements of ``migrations/0002_grants.sql`` so that code and database cannot disagree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from appmux.registry.writers import (
    WRITE_RULES,
    Identity,
    WriteIdentityError,
    WriteKind,
    require_identity,
)

GRANTS_PATH = Path(__file__).resolve().parents[2] / "migrations" / "0002_grants.sql"

SP = Identity.APPMUX_SP
ADMIN = Identity.PLATFORM_ADMIN

# Data model section 6, written out by hand: the one place the test restates the document.
EXPECTED_IDENTITIES: dict[WriteKind, frozenset[Identity]] = {
    WriteKind.PLATFORM_SETTING_INSERT: frozenset({ADMIN}),
    WriteKind.PLATFORM_SETTING_UPDATE: frozenset({ADMIN}),
    WriteKind.PLATFORM_SETTING_DELETE: frozenset({ADMIN}),
    WriteKind.APP_INSERT: frozenset({ADMIN}),
    WriteKind.APP_UPDATE: frozenset({ADMIN}),
    WriteKind.APP_DELETE: frozenset({ADMIN}),
    WriteKind.REQUEST_CREATE: frozenset({SP}),
    WriteKind.REQUEST_WITHDRAW: frozenset({SP}),
    WriteKind.REQUEST_ANSWER: frozenset({ADMIN}),
    WriteKind.APP_CONFIG_INSERT: frozenset({ADMIN}),
    WriteKind.APP_CONFIG_UPDATE: frozenset({SP}),
    WriteKind.APP_GRANT_INSERT: frozenset({SP}),
    WriteKind.APP_GRANT_UPDATE: frozenset({SP}),
    WriteKind.APP_GRANT_DELETE: frozenset({SP}),
    WriteKind.RELEASE_INSERT: frozenset({SP}),
    WriteKind.RELEASE_UPDATE: frozenset({SP}),
    WriteKind.APP_RESOURCE_INSERT: frozenset({SP}),
    WriteKind.APP_RESOURCE_UPDATE: frozenset({SP}),
    WriteKind.APP_RESOURCE_DELETE: frozenset({SP}),
    WriteKind.METRIC_INSERT: frozenset({SP}),
    WriteKind.METRIC_DELETE: frozenset({SP}),
    WriteKind.AUDIT_INSERT: frozenset({SP, ADMIN}),
}
PLATFORM_ONLY_KINDS = [kind for kind, who in EXPECTED_IDENTITIES.items() if who == {ADMIN}]
SP_ONLY_KINDS = [kind for kind, who in EXPECTED_IDENTITIES.items() if who == {SP}]

NINE_TABLES = {
    "platform_settings",
    "apps",
    "permission_requests",
    "app_config",
    "app_grants",
    "releases",
    "app_resources",
    "metrics",
    "audit_log",
}


def test_every_write_kind_routes_to_the_identity_of_the_data_model() -> None:
    assert {kind: rule.identities for kind, rule in WRITE_RULES.items()} == EXPECTED_IDENTITIES


def test_the_table_has_a_rule_for_every_write_kind_and_no_other() -> None:
    assert set(WRITE_RULES) == set(WriteKind)


def test_the_two_identities_are_named_after_their_database_roles() -> None:
    assert ADMIN.value == "appmux_platform_writers"
    assert SP.value == "appmux_sp"


def test_the_rules_cover_the_nine_tables_and_no_other() -> None:
    assert {rule.table for rule in WRITE_RULES.values()} == NINE_TABLES


@pytest.mark.parametrize("kind", PLATFORM_ONLY_KINDS)
def test_a_platform_table_write_with_the_service_principal_raises(kind: WriteKind) -> None:
    with pytest.raises(WriteIdentityError) as refusal:
        require_identity(kind, SP)
    assert kind.value in str(refusal.value)


@pytest.mark.parametrize("kind", SP_ONLY_KINDS)
def test_a_service_principal_table_write_with_a_platform_admin_raises(kind: WriteKind) -> None:
    with pytest.raises(WriteIdentityError) as refusal:
        require_identity(kind, ADMIN)
    assert kind.value in str(refusal.value)


@pytest.mark.parametrize("kind", list(WriteKind))
def test_the_right_identity_passes(kind: WriteKind) -> None:
    for identity in EXPECTED_IDENTITIES[kind]:
        require_identity(kind, identity)


def test_the_audit_log_accepts_either_identity() -> None:
    require_identity(WriteKind.AUDIT_INSERT, SP)
    require_identity(WriteKind.AUDIT_INSERT, ADMIN)


@pytest.mark.parametrize(
    "not_an_identity", ["", "root", "appmux_superuser", "databricks_superuser"]
)
def test_a_role_that_is_neither_identity_raises_for_every_kind(not_an_identity: Any) -> None:
    # Typed Any on purpose: the point is a caller that hands over something that is not an Identity.
    for kind in WriteKind:
        with pytest.raises(WriteIdentityError):
            require_identity(kind, not_an_identity)


def test_a_request_is_asked_and_withdrawn_by_the_service_principal_and_answered_by_an_admin() -> (
    None
):
    # The one table both classes write (data model 4.3): the row grants nothing.
    assert WRITE_RULES[WriteKind.REQUEST_CREATE].identities == {SP}
    assert WRITE_RULES[WriteKind.REQUEST_WITHDRAW].identities == {SP}
    assert WRITE_RULES[WriteKind.REQUEST_ANSWER].identities == {ADMIN}
    tables = {WRITE_RULES[kind].table for kind in WRITE_RULES if kind.name.startswith("REQUEST_")}
    assert tables == {"permission_requests"}


def test_no_write_kind_can_change_or_remove_an_audit_row() -> None:
    privileges = {rule.privilege for rule in WRITE_RULES.values() if rule.table == "audit_log"}
    assert privileges == {"INSERT"}


def test_no_write_kind_lets_the_service_principal_write_apps_or_platform_settings() -> None:
    for rule in WRITE_RULES.values():
        if rule.table in {"apps", "platform_settings"}:
            assert rule.identities == {ADMIN}


def _grants_of(sql_text: str) -> set[tuple[str, str, str]]:
    """The (table, privilege, role) triples that GRANT statements give, SELECT left out.

    SQL keywords are case-insensitive, and so are unquoted identifiers (PostgreSQL folds them to
    lower case), so privileges come back in upper case and tables and roles in lower case. A
    quoted identifier would keep its case; the migration quotes none, which a test below checks.
    """
    without_comments = "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())
    triples: set[tuple[str, str, str]] = set()
    for statement in without_comments.split(";"):
        match = re.fullmatch(
            r"\s*GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+(.+?)\s*",
            statement,
            re.DOTALL | re.IGNORECASE,
        )
        if match is None:
            continue
        privileges_text, tables_text, roles_text = match.groups()
        privileges = [item.strip().upper() for item in privileges_text.split(",")]
        tables = [item.strip().lower() for item in tables_text.split(",")]
        roles = [item.strip().lower() for item in roles_text.split(",")]
        if privileges == ["SELECT"]:
            continue
        triples.update(
            (table, privilege, role)
            for table in tables
            for privilege in privileges
            for role in roles
        )
    return triples


def _grants_of_the_rules() -> set[tuple[str, str, str]]:
    return {
        (rule.table, rule.privilege, identity.value)
        for rule in WRITE_RULES.values()
        for identity in rule.identities
    }


def test_the_write_table_equals_the_grants_of_the_migration() -> None:
    assert _grants_of_the_rules() == _grants_of(GRANTS_PATH.read_text())


@pytest.mark.parametrize(
    "statement",
    [
        "grant delete on audit_log to appmux_sp;",
        "Grant Delete On Audit_Log To APPMUX_SP;",
        "GRANT DELETE ON AUDIT_LOG TO APPMUX_SP;",
    ],
)
def test_the_grants_parser_reads_a_statement_in_any_letter_case(statement: str) -> None:
    assert _grants_of(statement) == {("audit_log", "DELETE", "appmux_sp")}


@pytest.mark.parametrize(
    "extra_grant",
    [
        "grant delete on audit_log to appmux_sp;",
        "grant update on audit_log to appmux_platform_writers;",
        "grant insert, update, delete on apps to appmux_sp;",
    ],
)
def test_a_lower_case_grant_added_to_the_migration_breaks_the_comparison(extra_grant: str) -> None:
    changed_migration = GRANTS_PATH.read_text() + "\n" + extra_grant + "\n"
    assert _grants_of(changed_migration) != _grants_of_the_rules()


def test_both_identities_may_read_every_table() -> None:
    text = GRANTS_PATH.read_text()
    assert re.search(
        r"GRANT SELECT ON ALL TABLES IN SCHEMA public TO appmux_sp, appmux_platform_writers;", text
    )


def test_the_grants_file_creates_no_role_and_gives_no_other_privilege() -> None:
    text = "\n".join(line.split("--", 1)[0] for line in GRANTS_PATH.read_text().splitlines())
    assert "CREATE ROLE" not in text.upper()
    assert "WITH GRANT OPTION" not in text.upper()
    assert "TO PUBLIC" not in text.upper()
    # An unquoted identifier folds to lower case, which is what _grants_of assumes.
    assert '"' not in text
    revokes = re.findall(r"REVOKE\s+(.+?)\s+FROM\s+(\w+)", text, re.DOTALL | re.IGNORECASE)
    assert revokes == [("UPDATE, DELETE ON audit_log", "PUBLIC")]
