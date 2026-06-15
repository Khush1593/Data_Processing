"""Stage 0.5 — Cross-Table Consistency Layer (stage0_v3_spec.md).

Runs once per project, after every table's v3.0 column-wise processing is
done but before locking/cold start. Operates ONLY on metadata already
computed during v3.0 profiling — column name, declared type,
``inferred_issues``, ``date_format``, and ``format_signature`` — never on raw
row values. Fully deterministic: no LLM calls, no additional sampling.

Three steps:
  find_groups()  — Step A+B: group same-kind columns across tables (dates,
                   phones, ID/key columns) and pick a canonical format per
                   group (majority rule weighted by row count, with
                   PK-preference for ID groups).
  make_patcher() — Step C: returns a per-table ``expression_patch`` callable
                   (see ``profiler.build_cleaning_script``) that deterministically
                   extends a column's existing expression to reach the
                   canonical format, or leaves it as-is + adds a note if a
                   safe conversion isn't possible.
  build_summary()— project-level summary dicts for the review UI.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from app.preprocessing.column_classifier import _is_identifier, _name_tokens
from app.preprocessing.models import ColumnExpression, ColumnMetadata, TableMetadata

PHONE_NAME_TOKENS: frozenset[str] = frozenset({"phone", "mobile", "cell", "tel", "fax", "whatsapp"})

_NUMERIC_DECLARED_TOKENS = ("int", "numeric", "decimal", "serial")


def _is_numeric_declared(declared_type: str) -> bool:
    dt = declared_type.lower()
    return any(t in dt for t in _NUMERIC_DECLARED_TOKENS)


@dataclass
class GroupMember:
    table: str
    column: str
    current_format: str  # format_signature: date/phone pattern, or "numeric"/"alnum" for ID group
    row_count: int
    is_primary_key: bool = False
    declared_type: str = ""


@dataclass
class ConsistencyGroup:
    group_type: str  # "date" | "phone" | "id"
    label: str
    canonical_format: str
    canonical_reason: str
    members: list[GroupMember] = field(default_factory=list)
    _tables_matching: list[str] | None = None
    _tables_needing_patch: list[str] | None = None

    @property
    def tables_matching(self) -> list[str]:
        if self._tables_matching is not None:
            return self._tables_matching
        return sorted({m.table for m in self.members if m.current_format == self.canonical_format})

    @property
    def tables_needing_patch(self) -> list[str]:
        if self._tables_needing_patch is not None:
            return self._tables_needing_patch
        return sorted({m.table for m in self.members if m.current_format != self.canonical_format})


def _weighted_majority(members: list[GroupMember], prefer: Callable[[str], bool] | None = None) -> str:
    weights: Counter[str] = Counter()
    for m in members:
        weights[m.current_format] += max(m.row_count, 1)
    best = weights.most_common()
    top_count = best[0][1]
    tied = [fmt for fmt, count in best if count == top_count]
    if len(tied) > 1 and prefer is not None:
        for fmt in tied:
            if prefer(fmt):
                return fmt
    return best[0][0]


def find_groups(tables: dict[str, TableMetadata]) -> list[ConsistencyGroup]:
    """Group same-kind columns across tables and pick a canonical format for
    each group. Conservative: only emits a group when 2+ columns from
    *different* tables qualify."""
    date_members: dict[str, list[GroupMember]] = {}
    phone_members: dict[str, list[GroupMember]] = {}
    id_members: dict[str, list[GroupMember]] = {}

    for table_name, metadata in tables.items():
        for col in metadata.columns:
            if col.format_signature and (_name_tokens(col.name) & PHONE_NAME_TOKENS):
                phone_members.setdefault(_label_key(col.name), []).append(
                    GroupMember(table_name, col.name, col.format_signature, metadata.row_count, col.is_primary_key)
                )
                continue

            if col.format_signature and (
                "mixed_date_format" in col.inferred_issues
                or "timestamp" in col.declared_type.lower()
                or "datetime" in col.declared_type.lower()
                or "date" in col.declared_type.lower()
                or (_name_tokens(col.name) & {"date", "time", "at", "on", "timestamp", "datetime"})
            ):
                # v3.0's column-wise cleaning already converts any date column
                # with a determinable format (declared TIMESTAMP/DATE, or a
                # detected strptime format) to a native TIMESTAMP — so there
                # is no real output-format difference between e.g.
                # "%d/%m/%Y" and "%Y-%m-%d" after cleaning. Only "ambiguous"
                # columns (no determinable format) remain inconsistent.
                normalized = "ambiguous" if col.format_signature == "ambiguous" else "native_timestamp"
                date_members.setdefault(_label_key(col.name), []).append(
                    GroupMember(table_name, col.name, normalized, metadata.row_count, col.is_primary_key)
                )
                continue

            if _is_identifier(col) and col.format_signature:
                id_members.setdefault(col.name.lower(), []).append(
                    GroupMember(
                        table_name, col.name, col.format_signature, metadata.row_count,
                        col.is_primary_key, declared_type=col.declared_type,
                    )
                )

    groups: list[ConsistencyGroup] = []

    for label, members in date_members.items():
        if len({m.table for m in members}) < 2:
            continue
        canonical = _weighted_majority(members, prefer=lambda f: f == "native_timestamp")
        reason = (
            "majority of rows already store this as a native timestamp after cleaning"
            if canonical == "native_timestamp"
            else "majority of rows have no determinable date format"
        )
        groups.append(ConsistencyGroup("date", f"Date columns matching '{label}'", canonical, reason, members))

    for label, members in phone_members.items():
        if len({m.table for m in members}) < 2:
            continue
        canonical = _weighted_majority(members, prefer=lambda f: f.startswith("intl"))
        reason = "majority of rows already use this format"
        groups.append(ConsistencyGroup("phone", f"Phone columns matching '{label}'", canonical, reason, members))

    for label, members in id_members.items():
        if len({m.table for m in members}) < 2:
            continue
        # Stage 0.5 Alphanumeric ID Guard (second addendum): a native numeric
        # declared_type anywhere in the group is sufficient evidence the
        # group is numeric-natured. Otherwise, any sampled letters anywhere
        # in the group mean it's UUID/hash/hex-like — never zero-strip those.
        if any(_is_numeric_declared(m.declared_type) for m in members):
            canonical = "numeric"
            numeric_member = next(m for m in members if _is_numeric_declared(m.declared_type))
            reason = f"native numeric declared type in table '{numeric_member.table}'"
        elif any(m.current_format == "alnum" for m in members):
            canonical = "alnum"
            reason = "letters found in sampled ID values across the group (UUID/hash-like)"
        else:
            canonical = "numeric"
            reason = "no letters found in sampled ID values across the group"

        # Every member currently gets a SELECT-as-is passthrough (no
        # trim/zero-strip normalization yet) — Stage 0.5 always patches all
        # of them to the canonical text form.
        groups.append(ConsistencyGroup(
            "id", f"ID/key columns named '{label}'", canonical, reason, members,
            _tables_matching=[], _tables_needing_patch=sorted({m.table for m in members}),
        ))

    return [g for g in groups if g.tables_needing_patch]


def _label_key(col_name: str) -> str:
    """Normalize a column name into a grouping key by stripping common
    prefixes/suffixes around a shared core token, e.g. 'order_date' and
    'created_date' both -> 'date'; 'customer_id' and 'cust_id' won't collide
    here (handled separately by the ID-group's exact-name grouping)."""
    tokens = _name_tokens(col_name)
    for core in ("date", "time", "phone", "mobile", "cell", "tel", "fax", "whatsapp"):
        if core in tokens:
            return core
    return col_name.lower()


# ---------------------------------------------------------------------------
# Step C — patch expressions for tables that don't match the canonical format
# ---------------------------------------------------------------------------

def _date_patch_expr(col_expr: str, current_sig: str, canonical_sig: str) -> str | None:
    """Date columns: v3.0's column-wise cleaning already converts any column
    with a determinable format to a native TIMESTAMP, so "native_timestamp"
    vs. a known strptime format are not actually different outputs. The only
    real mismatch is an "ambiguous" column (no determinable format) next to
    others that are native_timestamp — and that can't be safely converted
    without re-sampling/re-resolving, so always leave as-is (caller adds a
    note for the user)."""
    return None


def _phone_patch_expr(col_expr: str, current_sig: str, canonical_sig: str) -> str | None:
    if current_sig == canonical_sig:
        return None
    # Same digit count, current just has separators/punctuation -> strip them.
    if (
        current_sig.endswith("d") and canonical_sig.endswith("d")
        and current_sig.split("_")[-1] == canonical_sig.split("_")[-1]
        and current_sig.startswith("local") and canonical_sig.startswith("local")
    ):
        return f"REGEXP_REPLACE({col_expr}, '[^0-9]', '', 'g')"
    # Any other conversion (e.g. adding a country code) can't be done safely
    # without fabricating data.
    return None


def _id_patch_expr(existing: str, current_sig: str, canonical_sig: str) -> tuple[str, str | None]:
    """Returns ``(new_expr, note)``. ``note`` is set when the canonical
    zero-stripping couldn't be applied to this member (e.g. it's alnum/
    hash-like) so the user can review.

    Hard rule (Stage 0.5 Alphanumeric ID Guard): the zero-strip regex must
    NEVER run on a column whose sampled values contain letters — stripping a
    leading '0' from a hex/UUID/hash value silently mutates it.
    """
    base = f"TRIM(CAST(({existing}) AS VARCHAR))"
    if current_sig == "alnum":
        note = None
        if canonical_sig == "numeric":
            note = (
                "cross_table_alignment: this column contains non-numeric "
                "(UUID/hash-like) values, so leading-zero stripping was NOT "
                "applied — only trimmed/cast to VARCHAR for consistency"
            )
        return base, note
    # current_sig == "numeric"
    if canonical_sig == "numeric":
        return f"REGEXP_REPLACE({base}, '^0+(?=[0-9])', '')", None
    # canonical is "alnum" — keep this numeric member's output as plain
    # trimmed text (no zero-strip) to match the no-content-transform rule.
    return base, None


def make_patcher(
    table_name: str, groups: list[ConsistencyGroup], metadata: TableMetadata,
) -> Callable[[ColumnExpression, ColumnMetadata], ColumnExpression] | None:
    """Returns an ``expression_patch`` callable for ``build_cleaning_script``,
    or ``None`` if this table needs no patching for any group."""
    patches: dict[str, tuple[ConsistencyGroup, GroupMember]] = {}
    for group in groups:
        if table_name not in group.tables_needing_patch:
            continue
        for m in group.members:
            if m.table == table_name:
                if group.group_type == "id" or m.current_format != group.canonical_format:
                    patches[m.column] = (group, m)

    if not patches:
        return None

    def _patch(expr: ColumnExpression, col: ColumnMetadata) -> ColumnExpression:
        if col.name not in patches:
            return expr
        group, member = patches[col.name]
        existing = expr.sql_exprs[0] if expr.sql_exprs else None
        if existing is None:
            return expr

        new_expr: str | None = None
        extra_note: str | None = None
        if group.group_type == "date":
            new_expr = _date_patch_expr(existing, member.current_format, group.canonical_format)
        elif group.group_type == "phone":
            new_expr = _phone_patch_expr(existing, member.current_format, group.canonical_format)
        elif group.group_type == "id":
            new_expr, extra_note = _id_patch_expr(existing, member.current_format, group.canonical_format)

        if new_expr is None:
            note = (
                f"cross_table_alignment_needed: {group.label} canonical format is "
                f"'{group.canonical_format}', this table's is "
                f"'{member.current_format}' — needs manual review"
            )
            return ColumnExpression(
                col_name=expr.col_name,
                output_names=expr.output_names,
                sql_exprs=expr.sql_exprs,
                source=expr.source,
                issues_handled=[*expr.issues_handled, note],
                clarification_needed=expr.clarification_needed,
                clarification_question=expr.clarification_question,
                clarification_options=expr.clarification_options,
            )

        notes = [f"cross_table_alignment: aligned to {group.canonical_format}"]
        if extra_note:
            notes.append(extra_note)
        return ColumnExpression(
            col_name=expr.col_name,
            output_names=expr.output_names,
            sql_exprs=[new_expr],
            source=expr.source,
            issues_handled=[*expr.issues_handled, *notes],
            clarification_needed=expr.clarification_needed,
            clarification_question=expr.clarification_question,
            clarification_options=expr.clarification_options,
        )

    return _patch


def build_summary(groups: list[ConsistencyGroup]) -> list[dict]:
    return [
        {
            "group_type": g.group_type,
            "label": g.label,
            "canonical_format": g.canonical_format,
            "canonical_reason": g.canonical_reason,
            "tables_matching": g.tables_matching,
            "tables_needing_patch": g.tables_needing_patch,
        }
        for g in groups
    ]
