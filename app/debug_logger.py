"""Per-(project, table) debug log writer for the Stage 0 analysis pipeline.

When ``PREPROCESSING_DEBUG_LOG`` is enabled, every step of analyze/approve —
profiling, issue detection, the LLM prompt + raw response, AST validation,
and the dry-run diff — is appended as a section to a per-table markdown file
under ``DEBUG_LOG_DIR``, so the full pipeline can be reviewed end-to-end
after the fact.

Disabled by default and a no-op when disabled, so call sites can always
construct/use one without checking a flag first.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from app.config import get_settings

_lock = threading.Lock()


def new_run_id() -> str:
    """A timestamp identifying one analysis/approve run, used as the per-run
    folder name. Generate ONCE per run (in the orchestrator) and pass it to
    every table's :class:`DebugLogger` so all tables of the same run — which
    are profiled concurrently — land in the same folder."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


class DebugLogger:
    def __init__(
        self, project_id: str, table_name: str, run_id: str | None = None
    ) -> None:
        settings = get_settings()
        self.enabled = settings.PREPROCESSING_DEBUG_LOG
        self.path: str | None = None
        if not self.enabled:
            return

        # Layout: DEBUG_LOG_DIR/<project_id>/<run_timestamp>/<table>.md
        # so logs are grouped per project, and each run gets its own
        # timestamped folder — scales cleanly to many projects/runs instead
        # of dumping every md file into one flat directory.
        run_id = run_id or new_run_id()
        run_dir = os.path.join(
            settings.DEBUG_LOG_DIR, _sanitize(project_id), _sanitize(run_id)
        )
        os.makedirs(run_dir, exist_ok=True)
        self.path = os.path.join(run_dir, f"{_sanitize(table_name)}.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"# Debug log — project `{project_id}`, table `{table_name}`\n\n")
            f.write(f"- Run: {run_id}\n")
            f.write(f"- Started: {datetime.now(timezone.utc).isoformat()}\n\n")

    def _append(self, text: str) -> None:
        if not self.enabled or not self.path:
            return
        with _lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(text)

    def section(self, title: str, body: str = "") -> None:
        """Append a free-text section."""
        self._append(f"## {title}\n\n{body}\n\n" if body else f"## {title}\n\n")

    def code(self, title: str, content, lang: str = "") -> None:
        """Append a section whose body is a fenced code block. Non-string
        content (dicts, lists, pydantic models via ``default=str``) is
        JSON-serialised."""
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, default=str)
        self._append(f"## {title}\n\n```{lang}\n{content}\n```\n\n")

    def llm_call(
        self,
        title: str,
        prompt: str,
        raw_response: str | None = None,
        error: str | None = None,
    ) -> None:
        """Append a section showing the full prompt sent to the LLM and the
        raw (pre-validation) response or error it returned."""
        body = f"### Prompt\n\n```\n{prompt}\n```\n\n"
        if raw_response is not None:
            body += f"### Raw response\n\n```\n{raw_response}\n```\n\n"
        if error is not None:
            body += f"### Error\n\n```\n{error}\n```\n\n"
        self._append(f"## {title}\n\n{body}")


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
