"""Stage 0 v3.0 — Column Intelligence Gate (stage0_v3_spec.md §5).

Two-phase classification:
  pre_classify()  — name + declared type only. Called BEFORE sampling.
                    Final classification for SKIP classes (PII, IDENTIFIER,
                    STRUCTURAL) and for declared-BOOLEAN columns (OBSERVE).
                    Everything else is OBSERVE (pending issue detection).
  post_classify() — called AFTER issue detection on the sample. Upgrades a
                    pending OBSERVE column to CLEAN_DET / CLEAN_AMBIG / FREE_TEXT
                    / OBSERVE based on ``inferred_issues``.

``pre_classify`` tells the sampler which columns to include in the targeted
sample query (``needs_sample``) — PII/IDENTIFIER/STRUCTURAL columns are never
pulled into the sample DataFrame and never reach the LLM.
"""
from __future__ import annotations

from collections import Counter

from app.config import get_settings
from app.preprocessing.models import ClassifiedColumn, ColumnClass, ColumnMetadata

# ---------------------------------------------------------------------------
# Token sets
# ---------------------------------------------------------------------------

PII_NAME_TOKENS: frozenset[str] = frozenset({
    # Name
    "name", "fname", "lname", "firstname", "lastname", "fullname",
    "surname", "middlename", "initials",
    # Contact
    "email", "mail",
    "phone", "mobile", "cell", "tel", "fax", "whatsapp",
    # Address
    "address", "addr", "street", "road", "avenue", "lane",
    "locality", "city", "town", "district", "state", "province",
    "country", "zip", "postal", "postcode", "pincode",
    # Government / Identity
    "ssn", "sin", "passport", "license", "licence", "nid", "national",
    "dob", "birthdate", "birthday", "birth",
    "gender", "sex", "ethnicity", "race", "religion", "caste",
    # Network / Device
    "ip", "ipaddress", "mac", "imei", "deviceid", "useragent",
    "session", "cookie",
    # Auth
    "password", "pwd", "secret", "token", "apikey", "pin",
})

ID_SUFFIXES: frozenset[str] = frozenset({
    "_id", "_key", "_ref", "_fk", "_pk", "_uuid",
    "_hash", "_no", "_num", "_number", "_code",
})

ID_PREFIXES: frozenset[str] = frozenset({"id_", "fk_", "pk_", "uuid_"})

STRUCTURAL_TYPE_TOKENS: frozenset[str] = frozenset({
    "json", "jsonb", "array", "bytea", "binary", "blob",
    "xml", "hstore", "inet", "cidr", "macaddr",
    "tsvector", "tsquery", "bit", "varbit",
})

DATA_CHANGING_ISSUES: frozenset[str] = frozenset({
    "currency_string",
    "percentage_string",
    "mixed_date_format",
    "numeric_as_string",
    "inconsistent_boolean",
    "null_variant",
    "needs_trim",
    "inconsistent_casing",
})


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _name_tokens(col_name: str) -> set[str]:
    """Split underscore/hyphen-delimited column name into lowercase tokens."""
    return set(col_name.lower().replace("-", "_").split("_"))


def _is_pii(col_name: str) -> bool:
    return bool(_name_tokens(col_name) & PII_NAME_TOKENS)


def _is_identifier(col: ColumnMetadata) -> bool:
    if col.is_primary_key:
        return True
    name = col.name.lower()
    if any(name.endswith(s) for s in ID_SUFFIXES):
        return True
    if any(name.startswith(p) for p in ID_PREFIXES):
        return True
    return False


def _is_structural(declared_type: str) -> bool:
    dt = declared_type.lower()
    return any(tok in dt for tok in STRUCTURAL_TYPE_TOKENS)


def _is_boolean_typed(declared_type: str) -> bool:
    """Already BOOLEAN — never apply boolean-detection heuristics.

    Root fix for the v2.0 TRIM(BOOLEAN) binder crash: a column declared
    BOOLEAN is already correctly typed and must never receive a boolean
    cleaning expression.
    """
    return "bool" in declared_type.lower()


def _is_free_text(col: ColumnMetadata) -> bool:
    """High-cardinality text with no issues — descriptions, comments, notes."""
    dt = col.declared_type.lower()
    is_text = "varchar" in dt or "char" in dt or "text" in dt or "string" in dt
    settings = get_settings()
    return (
        is_text
        and not col.inferred_issues
        and col.distinct_count >= settings.PREPROCESSING_FREE_TEXT_MIN_DISTINCT
        and col.distinct_sample_ratio > settings.PREPROCESSING_FREE_TEXT_CARDINALITY_RATIO
    )


def _is_ambiguous(col: ColumnMetadata, active_issues: set[str]) -> bool:
    """True if any active issue cannot be resolved deterministically — i.e.
    it genuinely requires LLM judgment or user clarification."""
    # Date: ambiguous when no column-wide format was determinable.
    if "mixed_date_format" in active_issues and col.date_format is None:
        return True
    # Currency: ambiguous when multiple distinct currency symbols are present.
    if "currency_string" in active_issues and len(col.currency_symbols) > 1:
        return True
    # Percentage (including mixed '%'-suffixed and bare values, e.g. '5.2%'
    # and '0.02' in the same column) is NOT ambiguous: build_expression's
    # magnitude-aware rule ('%' or bare > 1 -> /100, else leave as-is)
    # deterministically normalizes every case to a 0-1 fraction.
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pre_classify(col: ColumnMetadata) -> ClassifiedColumn:
    """Phase 1 — metadata-only classification, called BEFORE sampling.

    PII/IDENTIFIER/STRUCTURAL columns get their final classification here and
    are never sampled. A declared-BOOLEAN column is OBSERVE (already clean).
    Everything else is OBSERVE (pending), to be upgraded by ``post_classify``.
    """
    if _is_pii(col.name):
        return ClassifiedColumn(col, ColumnClass.PII, ["name matches PII pattern"], [])

    if _is_identifier(col):
        return ClassifiedColumn(col, ColumnClass.IDENTIFIER, ["identifier column"], [])

    if _is_structural(col.declared_type):
        return ClassifiedColumn(
            col, ColumnClass.STRUCTURAL, [f"structural type: {col.declared_type}"], []
        )

    if _is_boolean_typed(col.declared_type):
        return ClassifiedColumn(
            col, ColumnClass.OBSERVE, ["declared type is already BOOLEAN"], []
        )

    return ClassifiedColumn(col, ColumnClass.OBSERVE, ["pending issue detection"], [])


def post_classify(col: ColumnMetadata) -> ClassifiedColumn:
    """Phase 2 — called AFTER issue detection on the sample.

    Upgrades a pending OBSERVE column to CLEAN_DET, CLEAN_AMBIG, FREE_TEXT, or
    leaves it OBSERVE. Re-runs the SKIP checks too, since ``post_classify`` is
    also used as the single source of truth for the full table.
    """
    if _is_pii(col.name):
        return ClassifiedColumn(col, ColumnClass.PII, ["name matches PII pattern"], [])
    if _is_identifier(col):
        return ClassifiedColumn(col, ColumnClass.IDENTIFIER, ["identifier column"], [])
    if _is_structural(col.declared_type):
        return ClassifiedColumn(
            col, ColumnClass.STRUCTURAL, [f"structural type: {col.declared_type}"], []
        )
    if _is_boolean_typed(col.declared_type):
        return ClassifiedColumn(
            col, ColumnClass.OBSERVE, ["declared type is already BOOLEAN"], []
        )
    if _is_free_text(col):
        return ClassifiedColumn(
            col, ColumnClass.FREE_TEXT, ["high-cardinality text, no issues"], []
        )

    active_issues = [i for i in (col.inferred_issues or []) if i in DATA_CHANGING_ISSUES]

    if not active_issues:
        return ClassifiedColumn(col, ColumnClass.OBSERVE, ["no data-changing issues"], [])

    if _is_ambiguous(col, set(active_issues)):
        return ClassifiedColumn(
            col, ColumnClass.CLEAN_AMBIG, ["ambiguous — requires LLM judgment"], active_issues
        )

    return ClassifiedColumn(
        col, ColumnClass.CLEAN_DET, ["deterministic rules fully applicable"], active_issues
    )


def classify_table(metadata) -> list[ClassifiedColumn]:
    """Post-sampling full-table classification."""
    return [post_classify(col) for col in metadata.columns]


def needs_sample(pre: ClassifiedColumn) -> bool:
    """True if this column should be included in the targeted sample query.

    Only OBSERVE (pending) columns get sampled. All SKIP classes
    (PII/IDENTIFIER/STRUCTURAL) and already-final OBSERVE-BOOLEAN columns are
    excluded — but a declared-BOOLEAN column's reason differs from a pending
    column's, so distinguish on the reason text.
    """
    if pre.classification != ColumnClass.OBSERVE:
        return False
    return pre.reasons == ["pending issue detection"]


def summary(classified: list[ClassifiedColumn]) -> dict[str, int]:
    """Human-readable count by class, for logs and the review UI."""
    return dict(Counter(c.classification.value for c in classified))
