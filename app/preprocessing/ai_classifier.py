"""Stage 0 v3.1 — Step 2: AI Metadata Gate.

Sends schema metadata (column names + declared types only, NO raw data)
for all tables in a project to the AI in a single call, then reconciles the
AI result with the deterministic ``pre_classify()`` results.

Reconciliation rules (Section 2.4):
  - Deterministic PII or IDENTIFIER is NEVER downgraded.
  - AI can only UPGRADE a deterministic OBSERVE to PII or IDENTIFIER.
  - Low-confidence AI results are discarded; deterministic result is kept.

On any AI failure the module falls back to the deterministic results
immediately without retry (Section 2.5).

Results are cached in-process per ``(project_id, schema_hash)`` so that
subsequent syncs with an unchanged schema skip the AI call (Section 2.6).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.llm_engine import _generate_structured
from app.preprocessing.column_classifier import pre_classify
from app.preprocessing.models import ColumnClass, TableMetadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process cache: (project_id, schema_hash) -> classifications
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[str, str], dict[str, dict[str, ColumnClass]]] = {}


# ---------------------------------------------------------------------------
# Pydantic response schema
# ---------------------------------------------------------------------------

class _ColumnAIResult(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    classification: Literal["PII", "IDENTIFIER", "OBSERVE"]
    confidence: Literal["high", "medium", "low"]
    reason: str = ""


class _AIClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tables: dict[str, dict[str, _ColumnAIResult]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema hash — invalidates cache when columns change
# ---------------------------------------------------------------------------

def _schema_hash(tables: dict[str, TableMetadata]) -> str:
    """Stable hash of (table_name, col_name, declared_type) tuples only."""
    items = []
    for tname in sorted(tables):
        for col in tables[tname].columns:
            items.append(f"{tname}:{col.name}:{col.declared_type}")
    return hashlib.sha1("\n".join(items).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a data privacy and schema analysis expert.
You will receive the schema of one or more database tables \
(column names and declared types only — no data values).

For each column in every table, classify it into exactly one of:

  PII        — contains or likely contains personal data: names, emails,
               phone numbers, addresses, dates of birth, government IDs,
               passwords, IP addresses, device identifiers, and similar.

  IDENTIFIER — a technical key with no analytical value: primary keys,
               foreign keys, UUIDs, hash codes, surrogate keys, SKUs,
               ticket numbers, tracking codes, session tokens, and similar.

  OBSERVE    — safe to sample and analyze: dates, amounts, statuses,
               scores, flags, categories, and any column that is neither
               PII nor a technical identifier.

Classification rules:
- Classify by column NAME and TABLE CONTEXT, not by declared SQL type alone.
  A VARCHAR named "customer_email" is PII. An INTEGER named "user_id" is
  IDENTIFIER.
- Use table context: in a "payments" table, "code" is likely a payment code
  (IDENTIFIER). In an "employees" table, "code" is likely an employee code
  (also IDENTIFIER). In a "discounts" table, "code" is a discount code
  (IDENTIFIER). Context matters.
- Classify non-English column names using their meaning: "nombre_cliente"
  is a customer name (PII), "monto_total" is a total amount (OBSERVE),
  "numero_telefono" is a phone number (PII).
- When uncertain between PII and OBSERVE, always choose PII.
- When uncertain between IDENTIFIER and OBSERVE, choose IDENTIFIER if the
  name suggests a reference or key, OBSERVE otherwise.

Respond ONLY with a JSON object matching this exact schema.
No explanation. No markdown fences. No extra keys.

{
  "tables": {
    "table_name": {
      "col_name": {
        "classification": "PII" | "IDENTIFIER" | "OBSERVE",
        "confidence": "high" | "medium" | "low",
        "reason": "one short phrase"
      }
    }
  }
}"""


def _build_schema_prompt(tables: dict[str, TableMetadata]) -> str:
    blocks = []
    for tname in sorted(tables):
        meta = tables[tname]
        col_lines = "\n".join(
            f"  - {col.name:<30} {col.declared_type}"
            for col in meta.columns
        )
        blocks.append(f"Table: {tname}\nColumns:\n{col_lines}")
    db_type = "Unknown"
    return f"Database type: {db_type}\n\n" + "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

_AI_TO_CLASS: dict[str, ColumnClass] = {
    "PII": ColumnClass.PII,
    "IDENTIFIER": ColumnClass.IDENTIFIER,
    "OBSERVE": ColumnClass.OBSERVE,
}

_SKIP_CLASSES = frozenset({ColumnClass.PII, ColumnClass.IDENTIFIER, ColumnClass.STRUCTURAL})


def _reconcile(det_class: ColumnClass, ai_result: Optional[_ColumnAIResult]) -> ColumnClass:
    """Apply reconciliation rules from Section 2.4."""
    # Deterministic PII/IDENTIFIER is never downgraded.
    if det_class in _SKIP_CLASSES:
        return det_class
    # Low-confidence AI result is too risky to act on.
    if ai_result is None or ai_result.confidence == "low":
        return det_class
    ai_class = _AI_TO_CLASS.get(ai_result.classification, ColumnClass.OBSERVE)
    # AI can only upgrade OBSERVE → PII or IDENTIFIER; it cannot downgrade.
    if det_class == ColumnClass.OBSERVE and ai_class in (ColumnClass.PII, ColumnClass.IDENTIFIER):
        return ai_class
    return det_class


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ai_classify_tables(
    tables: dict[str, TableMetadata],
    project_id: str = "",
    llm_provider: str | None = None,
    llm_model: str | None = None,
    api_key: str | None = None,
) -> dict[str, dict[str, ColumnClass]]:
    """Classify every column in every table via AI + deterministic reconciliation.

    Returns ``{table_name: {col_name: ColumnClass}}``.

    On any AI failure, falls back to deterministic ``pre_classify()`` for all
    tables immediately without blocking (Section 2.5). The result is cached
    in-process by ``(project_id, schema_hash)`` (Section 2.6).
    """
    if not tables:
        return {}

    schema_key = (project_id, _schema_hash(tables))
    if schema_key in _CACHE:
        logger.debug("AI classifier: cache hit for project %s", project_id)
        return _CACHE[schema_key]

    # Deterministic baseline — always computed, used for reconciliation and fallback.
    deterministic: dict[str, dict[str, ColumnClass]] = {}
    for tname, meta in tables.items():
        deterministic[tname] = {col.name: pre_classify(col).classification for col in meta.columns}

    prompt = f"{_SYSTEM_PROMPT}\n\nClassify the following schema:\n\n{_build_schema_prompt(tables)}"

    try:
        response: _AIClassificationResponse = _generate_structured(
            prompt=prompt,
            response_schema=_AIClassificationResponse,
            provider=llm_provider,
            model=llm_model,
            api_key=api_key,
            temperature=0.0,
        )

        result: dict[str, dict[str, ColumnClass]] = {}
        for tname, meta in tables.items():
            ai_table = response.tables.get(tname, {})
            result[tname] = {}
            for col in meta.columns:
                det = deterministic[tname][col.name]
                ai_col = ai_table.get(col.name)
                result[tname][col.name] = _reconcile(det, ai_col)

        logger.info(
            "AI classifier: classified %d tables, %d total columns",
            len(tables), sum(len(m.columns) for m in tables.values()),
        )

        # Log any upgrades for observability.
        for tname, cols in result.items():
            for cname, cls in cols.items():
                det = deterministic[tname][cname]
                if cls != det:
                    logger.info(
                        "AI classifier: %s.%s upgraded %s → %s",
                        tname, cname, det.value, cls.value,
                    )

        _CACHE[schema_key] = result
        return result

    except Exception as exc:
        logger.error(
            "AI classifier failed (%s) — falling back to deterministic pre_classify for all %d table(s).",
            exc, len(tables),
        )
        _CACHE[schema_key] = deterministic
        return deterministic


def invalidate_cache(project_id: str) -> None:
    """Remove all cached entries for a project (call when project is deleted)."""
    keys = [k for k in _CACHE if k[0] == project_id]
    for k in keys:
        del _CACHE[k]
