"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { api, DiffPage } from "@/lib/api";

const PAGE_SIZE = 25;

function formatVal(v: unknown): string {
  if (v === null || v === undefined) return "∅ null";
  if (typeof v === "number") return String(v);
  return String(v);
}

export default function TableComparePage() {
  const params = useParams<{ id: string; table: string }>();
  const { id: projectId, table } = params;

  const [page, setPage] = useState(1);
  const [data, setData] = useState<DiffPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await api.getDiff(projectId, table, page, PAGE_SIZE);
      setData(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId, table, page]);

  useEffect(() => {
    load();
  }, [load]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total_changed / data.page_size)) : 1;

  return (
    <div className="min-h-screen bg-zinc-50">
    <div className="mx-auto max-w-6xl px-4 py-10">
      <Link
        href={`/projects/${projectId}`}
        className="text-sm text-zinc-500 underline-offset-2 hover:underline"
      >
        ← Back to project
      </Link>

      <h1 className="mt-2 text-2xl font-semibold text-zinc-900">
        {table} — before / after
      </h1>

      {error && (
        <div className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
      )}

      {data && (
        <>
          <p className="mt-2 text-sm text-zinc-500">
            {data.total_changed.toLocaleString()} row(s) changed by the cleaning script. Showing
            page {data.page} of {totalPages}. Unchanged rows are not shown.
          </p>

          <div className="mt-6 space-y-6">
            {data.rows.length === 0 && (
              <p className="text-sm text-zinc-400">No changed rows on this page.</p>
            )}
            {data.rows.map((row) => (
              <div
                key={String(row.key)}
                className="overflow-hidden rounded-lg border border-zinc-200 bg-white shadow-sm"
              >
                <div className="border-b border-zinc-100 bg-zinc-50 px-4 py-2 text-xs font-medium text-zinc-500">
                  Row key: <span className="font-mono">{String(row.key)}</span>
                </div>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-zinc-50 text-left text-xs uppercase tracking-wide text-zinc-400">
                      <th className="px-4 py-2 font-medium">Column</th>
                      <th className="px-4 py-2 font-medium">Before</th>
                      <th className="px-4 py-2 font-medium">After</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.columns.map((col) => {
                      const changed = row.changed_columns.includes(col);
                      return (
                        <tr
                          key={col}
                          className={`border-t border-zinc-100 ${
                            changed ? "" : "text-zinc-400"
                          }`}
                        >
                          <td className="px-4 py-1.5 font-mono text-xs">{col}</td>
                          <td className="px-4 py-1.5 font-mono text-xs">
                            {changed ? (
                              <span className="rounded bg-red-50 px-1.5 py-0.5 text-red-700 line-through decoration-red-400">
                                {formatVal(row.before[col])}
                              </span>
                            ) : (
                              formatVal(row.before[col])
                            )}
                          </td>
                          <td className="px-4 py-1.5 font-mono text-xs">
                            {changed ? (
                              <span className="rounded bg-green-50 px-1.5 py-0.5 font-medium text-green-700">
                                {formatVal(row.after[col])}
                              </span>
                            ) : (
                              formatVal(row.after[col])
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ))}
          </div>

          <div className="mt-6 flex items-center justify-between">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1 || loading}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm disabled:opacity-40"
            >
              ← Previous
            </button>
            <span className="text-sm text-zinc-500">
              Page {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages || loading}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm disabled:opacity-40"
            >
              Next →
            </button>
          </div>
        </>
      )}
    </div>
    </div>
  );
}
