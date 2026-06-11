"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

const SAMPLE_URI = "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres";
const SAMPLE_SCHEMA = "clarum_test";

export default function Home() {
  const router = useRouter();
  const [dbUri, setDbUri] = useState("");
  const [schema, setSchema] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const { project_id } = await api.createProject(dbUri.trim(), schema.trim() || null);
      router.push(`/projects/${project_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setLoading(false);
    }
  }

  function fillSample() {
    setDbUri(SAMPLE_URI);
    setSchema(SAMPLE_SCHEMA);
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-gradient-to-b from-zinc-100 to-zinc-50 px-4 py-16">
      <div className="mb-8 text-center">
        <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-zinc-900 text-lg font-bold text-white">
          C
        </div>
        <h1 className="text-3xl font-bold tracking-tight text-zinc-900">Clarum Insights</h1>
        <p className="mt-1 text-sm font-medium uppercase tracking-widest text-zinc-400">
          Stage 0 — Data Pre-processing Review
        </p>
      </div>

      <div className="w-full max-w-xl rounded-2xl border border-zinc-200 bg-white p-8 shadow-lg shadow-zinc-200/50">
        <p className="text-sm leading-relaxed text-zinc-600">
          Connect a source database. We&apos;ll analyse every table, propose AI-generated
          cleaning scripts with a plain-English explanation, and let you review every
          change — before/after, row by row — prior to approval.
        </p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <div>
            <label className="block text-sm font-medium text-zinc-700">
              Database connection URI
            </label>
            <input
              type="text"
              required
              value={dbUri}
              onChange={(e) => setDbUri(e.target.value)}
              placeholder="postgresql+psycopg2://user:pass@host:port/dbname"
              spellCheck={false}
              autoComplete="off"
              className="mt-1 w-full rounded-md border border-zinc-300 px-3 py-2 font-mono text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-zinc-500 focus:outline-none focus:ring-1 focus:ring-zinc-500"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-zinc-700">
              Schema <span className="text-zinc-400">(optional)</span>
            </label>
            <input
              type="text"
              value={schema}
              onChange={(e) => setSchema(e.target.value)}
              placeholder="public"
              spellCheck={false}
              autoComplete="off"
              className="mt-1 w-full rounded-md border border-zinc-300 px-3 py-2 font-mono text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-zinc-500 focus:outline-none focus:ring-1 focus:ring-zinc-500"
            />
          </div>

          {error && (
            <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-md bg-zinc-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
          >
            {loading ? "Starting analysis…" : "Analyze database"}
          </button>
        </form>

        <button
          type="button"
          onClick={fillSample}
          className="mt-3 w-full rounded-md border border-dashed border-zinc-300 px-4 py-2 text-xs font-medium text-zinc-500 transition-colors hover:border-zinc-400 hover:text-zinc-700"
        >
          Use sample dataset (clarum_test)
        </button>
      </div>
    </div>
  );
}
