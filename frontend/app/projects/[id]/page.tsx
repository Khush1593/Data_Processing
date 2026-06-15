"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { api, ClarificationAnswer, ProjectStatus, TableDetail } from "@/lib/api";

const STATUS_BADGE: Record<string, string> = {
  analyzing: "bg-amber-100 text-amber-800",
  approving: "bg-amber-100 text-amber-800",
  ready: "bg-blue-100 text-blue-800",
  completed: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
  analyzed: "bg-blue-100 text-blue-800",
  cold_start_running: "bg-amber-100 text-amber-800",
  cold_start_done: "bg-green-100 text-green-800",
};

function Badge({ text }: { text: string }) {
  return (
    <span
      className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${
        STATUS_BADGE[text] || "bg-zinc-100 text-zinc-700"
      }`}
    >
      {text}
    </span>
  );
}

function TableCard({
  projectId,
  tableName,
  summary,
  answers,
  onAnswerChange,
}: {
  projectId: string;
  tableName: string;
  summary: ProjectStatus["tables"][number];
  answers: Record<string, ClarificationAnswer>;
  onAnswerChange: (column: string, optionId: string, note?: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<TableDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  async function toggleOpen() {
    if (!open && !detail) {
      setLoadingDetail(true);
      try {
        const d = await api.getTableDetail(projectId, tableName);
        setDetail(d);
      } finally {
        setLoadingDetail(false);
      }
    }
    setOpen(!open);
  }

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-zinc-900">{summary.table_name}</h3>
          <p className="text-sm text-zinc-500">
            {summary.row_count?.toLocaleString()} rows · sync mode: {summary.sync_mode}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {summary.safe_to_lock === false && <Badge text="WARN" />}
          {summary.safe_to_lock === true && <Badge text="OK" />}
          <Badge text={summary.status} />
        </div>
      </div>

      {summary.issues.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {summary.issues.map((issue) => (
            <span
              key={issue.column}
              className="rounded-full bg-zinc-100 px-2 py-0.5 text-xs text-zinc-700"
            >
              <span className="font-medium">{issue.column}</span>: {issue.issue}
            </span>
          ))}
        </div>
      )}

      {summary.warnings.length > 0 && (
        <div className="mt-3 space-y-1">
          {summary.warnings.map((w, i) => (
            <p key={i} className="text-xs text-amber-700">
              ⚠ {w}
            </p>
          ))}
        </div>
      )}

      {summary.clarifications.length > 0 && summary.status === "analyzed" && (
        <div className="mt-3 space-y-3">
          {summary.clarifications.map((c) => (
            <div
              key={c.column}
              className="rounded-md border border-amber-200 bg-amber-50 p-3"
            >
              <p className="text-sm font-medium text-amber-900">
                ❓ {c.question}
              </p>
              <div className="mt-2 space-y-1.5">
                {c.options.map((opt) => {
                  const selected = (answers[c.column]?.option || c.default) === opt.id;
                  return (
                    <div key={opt.id}>
                      <label className="flex items-start gap-2 text-sm text-amber-900">
                        <input
                          type="radio"
                          name={`${tableName}-${c.column}`}
                          value={opt.id}
                          checked={selected}
                          onChange={() =>
                            onAnswerChange(c.column, opt.id, answers[c.column]?.note)
                          }
                          className="mt-1"
                        />
                        <span>{opt.label}</span>
                      </label>
                      {opt.requires_note && selected && (
                        <textarea
                          value={answers[c.column]?.note || ""}
                          onChange={(e) => onAnswerChange(c.column, opt.id, e.target.value)}
                          placeholder="e.g. Convert everything to USD. EUR = 1.08 USD, GBP = 1.27 USD."
                          rows={2}
                          className="mt-1.5 ml-6 w-[calc(100%-1.5rem)] rounded-md border border-amber-300 bg-white px-2 py-1.5 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-amber-500 focus:outline-none focus:ring-1 focus:ring-amber-500"
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {summary.cold_start_error && (
        <p className="mt-3 text-xs text-red-700">Error: {summary.cold_start_error}</p>
      )}

      <div className="mt-4 flex items-center gap-3">
        <button
          onClick={toggleOpen}
          className="text-sm font-medium text-zinc-700 underline-offset-2 hover:underline"
        >
          {open ? "Hide details" : "View cleaning script & explanation"}
        </button>
        {summary.status === "cold_start_done" && (
          <Link
            href={`/projects/${projectId}/tables/${tableName}`}
            className="text-sm font-medium text-blue-700 underline-offset-2 hover:underline"
          >
            Compare before / after →
          </Link>
        )}
      </div>

      {open && (
        <div className="mt-4 space-y-3 border-t border-zinc-100 pt-4">
          {loadingDetail && <p className="text-sm text-zinc-400">Loading…</p>}
          {detail && (
            <>
              <div>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                  Explanation
                </h4>
                <p className="mt-1 text-sm text-zinc-700">{detail.explanation}</p>
              </div>
              <div>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                  Generated cleaning SQL
                </h4>
                <pre className="mt-1 overflow-x-auto rounded-md bg-zinc-900 p-3 text-xs text-zinc-100">
                  {detail.cleaning_sql}
                </pre>
              </div>
              <div>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                  Sample dry-run diff
                </h4>
                <table className="mt-1 w-full text-xs">
                  <thead>
                    <tr className="text-left text-zinc-400">
                      <th className="py-1 pr-3">Column</th>
                      <th className="py-1 pr-3">Type before → after</th>
                      <th className="py-1 pr-3">Nulls before → after</th>
                      <th className="py-1">Transformed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.diff.column_diffs?.map((c) => (
                      <tr key={c.column} className="border-t border-zinc-100 text-zinc-700">
                        <td className="py-1 pr-3 font-mono">{c.column}</td>
                        <td className="py-1 pr-3">
                          {c.type_before} → {c.type_after}
                        </td>
                        <td className="py-1 pr-3">
                          {c.null_before} → {c.null_after}
                        </td>
                        <td className="py-1">{c.transformed ? "yes" : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  const [project, setProject] = useState<ProjectStatus | null>(null);
  const [approving, setApproving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // table_name -> column_name -> chosen answer
  const [clarificationAnswers, setClarificationAnswers] = useState<
    Record<string, Record<string, ClarificationAnswer>>
  >({});

  const load = useCallback(async () => {
    try {
      const p = await api.getProject(projectId);
      setProject(p);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    load();
    const interval = setInterval(() => {
      load();
    }, 2500);
    return () => clearInterval(interval);
  }, [load]);

  async function handleApprove() {
    setApproving(true);
    try {
      await api.approveProject(projectId, undefined, clarificationAnswers);
      await load();
    } finally {
      setApproving(false);
    }
  }

  function handleAnswerChange(table: string, column: string, optionId: string, note?: string) {
    setClarificationAnswers((prev) => ({
      ...prev,
      [table]: { ...(prev[table] || {}), [column]: { option: optionId, note } },
    }));
  }

  if (err) {
    return <div className="p-8 text-red-700">Error: {err}</div>;
  }
  if (!project) {
    return <div className="p-8 text-zinc-500">Loading…</div>;
  }

  const isAnalyzing = project.status === "analyzing";
  const isApproving = project.status === "approving";
  const canApprove =
    project.status === "ready" &&
    project.tables.length > 0 &&
    project.tables.every((t) => t.status === "analyzed" || t.status === "failed");

  return (
    <div className="min-h-screen bg-zinc-50">
      <div className="mx-auto max-w-4xl px-4 py-10">
      <div className="mb-6 flex items-start justify-between">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-zinc-900 text-sm font-bold text-white">
            C
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-zinc-900">Stage 0 Review</h1>
            <p className="mt-1 font-mono text-xs text-zinc-500">{project.db_uri}</p>
            {project.schema && (
              <p className="font-mono text-xs text-zinc-500">schema: {project.schema}</p>
            )}
          </div>
        </div>
        <Badge text={project.status} />
      </div>

      {project.error && (
        <div className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {project.error}
        </div>
      )}

      {isAnalyzing && (
        <p className="mb-4 text-sm text-zinc-500">
          Analysing tables and generating cleaning scripts… this can take a minute.
        </p>
      )}
      {isApproving && (
        <p className="mb-4 text-sm text-zinc-500">
          Locking scripts and building the cleaned cache… this can take a while for large
          tables.
        </p>
      )}

      {canApprove && (
        <div className="mb-6">
          <button
            onClick={handleApprove}
            disabled={approving}
            className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            {approving ? "Starting…" : "Approve all & process"}
          </button>
        </div>
      )}

      {project.cross_table_summary && project.cross_table_summary.length > 0 && (
        <div className="mb-6 rounded-lg border border-zinc-200 bg-white p-5 shadow-sm">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Cross-table consistency
          </h2>
          <p className="mt-1 text-sm text-zinc-500">
            These columns appear in multiple tables. A single canonical format was chosen
            for each group, and the cleaning SQL for any non-matching table was adjusted to
            match — this is intentional cross-table alignment, not a mistake.
          </p>
          <div className="mt-3 space-y-3">
            {project.cross_table_summary.map((g, i) => (
              <div key={i} className="rounded-md border border-zinc-100 bg-zinc-50 p-3 text-sm">
                <p className="font-medium text-zinc-900">{g.label}</p>
                <p className="mt-0.5 text-zinc-600">
                  Canonical format: <span className="font-mono">{g.canonical_format}</span>{" "}
                  ({g.canonical_reason})
                </p>
                {g.tables_matching.length > 0 && (
                  <p className="mt-1 text-zinc-600">
                    Already matches: {g.tables_matching.join(", ")}
                  </p>
                )}
                {g.tables_needing_patch.length > 0 && (
                  <p className="mt-1 text-zinc-600">
                    Adjusted: {g.tables_needing_patch.join(", ")}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="space-y-4">
        {project.tables.map((t) => (
          <TableCard
            key={t.table_name}
            projectId={projectId}
            tableName={t.table_name}
            summary={t}
            answers={clarificationAnswers[t.table_name] || {}}
            onAnswerChange={(column, optionId, note) =>
              handleAnswerChange(t.table_name, column, optionId, note)
            }
          />
        ))}
      </div>
      </div>
    </div>
  );
}
