const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
}

export interface TableIssue {
  column: string;
  issue: string;
}

export interface ClarificationOption {
  id: string;
  label: string;
  requires_note?: boolean;
}

export interface ClarificationAnswer {
  option: string;
  note?: string;
}

export interface Clarification {
  column: string;
  question: string;
  options: ClarificationOption[];
  default: string;
}

export interface TableSummary {
  table_name: string;
  row_count: number;
  sync_mode: string;
  issues: TableIssue[];
  columns_transformed: string[];
  script_source: string;
  safe_to_lock: boolean | null;
  warnings: string[];
  status: string;
  cold_start_error: string | null;
  clarifications: Clarification[];
}

export interface CrossTableGroup {
  group_type: string;
  label: string;
  canonical_format: string;
  canonical_reason: string;
  tables_matching: string[];
  tables_needing_patch: string[];
}

export interface ProjectStatus {
  project_id: string;
  db_uri: string;
  schema: string | null;
  status: string;
  error: string | null;
  cross_table_summary: CrossTableGroup[];
  tables: TableSummary[];
}

export interface TableDetail {
  table_name: string;
  metadata: {
    row_count: number;
    columns: {
      name: string;
      declared_type: string;
      null_pct: number;
      distinct_count: number;
      sample_values: string[];
      inferred_issues: string[];
    }[];
  };
  cleaning_sql: string;
  explanation: string;
  columns_transformed: string[];
  diff: {
    row_count_before: number;
    row_count_after: number;
    safe_to_lock: boolean;
    warnings: string[];
    column_diffs: {
      column: string;
      null_before: number;
      null_after: number;
      type_before: string;
      type_after: string;
      transformed: boolean;
    }[];
  };
  script_source: string;
  status: string;
  cold_start_error: string | null;
}

export interface DiffRow {
  key: unknown;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  changed_columns: string[];
}

export interface DiffPage {
  table: string;
  page: number;
  page_size: number;
  total_changed: number;
  columns: string[];
  column_types: Record<string, { before: string; after: string }>;
  rows: DiffRow[];
}

export const api = {
  createProject: (db_uri: string, schema_name: string | null) =>
    request<{ project_id: string; status: string }>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ db_uri, schema_name }),
    }),

  getProject: (id: string) => request<ProjectStatus>(`/api/projects/${id}`),

  getTableDetail: (id: string, table: string) =>
    request<TableDetail>(`/api/projects/${id}/tables/${table}`),

  approveProject: (
    id: string,
    tables?: string[],
    clarificationAnswers?: Record<string, Record<string, ClarificationAnswer>>
  ) =>
    request<{ status: string }>(`/api/projects/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        tables: tables || null,
        clarification_answers: clarificationAnswers || null,
      }),
    }),

  getDiff: (id: string, table: string, page: number, pageSize = 25) =>
    request<DiffPage>(
      `/api/projects/${id}/tables/${table}/diff?page=${page}&page_size=${pageSize}`
    ),
};
