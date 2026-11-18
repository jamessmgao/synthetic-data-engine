// Response shapes of the pipeline API (api/app.py, api/service.py). Hand-written: the API
// returns plain dicts for most read endpoints, so there is no OpenAPI schema to generate from.

export type TaskStatus = "CREATED" | "CALIBRATING" | "ACCEPTED" | "REJECTED" | "CANCELLED";
export type JobStatus = "READY" | "LEASED" | "DONE" | "DEAD";
export type Outcome = "OK" | "FAILED" | "VALID_PASS" | "VALID_FAIL" | "INVALID";

export interface Health {
  status: string;
  db: string;
  budget?: {
    cap_usd: number | null;
    spent_usd: number;
    remaining_usd: number | null;
    budget_exhausted: boolean;
  };
  backends?: Record<string, string>;
}

export interface BatchSummary {
  batch_id: string;
  tasks: number;
  accepted: number;
  rejected: number;
  cancelled: number;
  in_progress: number;
  created_at: string;
  dev: boolean; // mock runs, or in the API's SDE_DEV_BATCHES
}

export interface TaskSummary {
  id: string;
  name: string | null;
  status: TaskStatus;
  reason: string | null;
  current_version: number;
  stage: string;
  pass_pattern: string;
  cost_usd: number | null;
  dev: boolean; // recorded cost exactly 0: a mock run (load test, prodmock)
  created_at: string;
}

export interface Batch {
  batch_id: string;
  counts_by_status: Record<string, number>;
  tasks: TaskSummary[];
}

export interface Breakdown {
  candidates: number;
  finished: number;
  accepted: number;
  rejected: number;
  learnable_rate: number | null;
  valid_rollouts: number;
  pass_rate: number | null;
}

export interface Report {
  candidates: number;
  structural_pass: number;
  functional_pass: number;
  accepted_v1: number;
  accepted_total: number;
  rejected: Record<string, number>;
  in_progress: number;
  cancelled: number;
  pass_count_histogram: Record<string, number>;
  valid_rollouts: number;
  invalid_rollouts: number;
  cost_usd: {
    authoring: number | null;
    evaluation: number | null;
    total: number | null;
    per_accepted: number | null;
  };
  wall_clock_s: number | null;
  by_family: Record<string, Breakdown>;
  by_mechanism: Record<string, Breakdown>;
  yield: { candidates: number; functional: number; accepted: number; accepted_v1: number; learnable_rate: number | null };
  scope: "prod" | "dev" | "all";
  other_scope: number; // tasks the other view (prod <-> dev) has
}

export interface Job {
  id: string;
  task_id: string;
  type: string;
  version: number;
  slot: number;
  retry: number;
  status: JobStatus;
  outcome: Outcome | null;
  attempts: number;
  lease_owner: string | null;
  bundle_sha256: string | null;
  error: string | null;
  trace_uri: string | null;
  cost_usd: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  origin: string | null;
}

export interface Queue {
  counts: Record<string, Partial<Record<JobStatus, number>>>;
  running: Job[];
}

export type Gate = boolean | ({ passed?: boolean } & Record<string, unknown>);

export interface Version {
  n: number;
  origin: string | null;
  operator: string | null;
  mutation_operators: string[];
  state: string;
  world: { world_sha256: string | null; lineage: unknown; status: string | null; outcome: string | null } | null;
  bundle_sha256: string | null;
  repairs: number;
  gates: Record<string, Gate> | null;
  pass_count: number | null;
  pass_pattern: string;
  cost_usd: number | null;
  leak_review: LeakReview | null; // the leak gate on the final bundle
  leak_attempts: LeakReview[]; // earlier drafts (a create or repair it sent back)
}

// The leak gate (validator/gates.py gate_leak) on one bundle: a semantic review of the
// instruction against the hidden fix, and a blind prediction from the instruction alone,
// judged against the real fix (only "specific" is a leak).
export interface LeakReview {
  job_id: string;
  job_type: string;
  origin: string | null;
  outcome: string | null;
  bundle_sha256: string | null;
  passed: boolean | null;
  verdict: string | null; // clean | leak | inconclusive
  leaks: { quote: string; reveals: string; severity: string }[] | null;
  unverified_quotes: { quote: string; reveals: string; severity: string }[] | null;
  direct: number | null;
  strong_hints: number | null;
  blind: {
    prediction?: { predicted_fix?: string; predicted_failure?: string; [k: string]: unknown } | null;
    judge?: { match: "specific" | "generic" | "none"; why: string } | null;
    skipped?: string;
    inconclusive?: boolean;
  } | null;
  blind_leak: boolean | null;
  cut_fields: string[] | null;
  inconclusive: string[] | null;
  skipped: string | null;
  cost_usd: number | null;
  adopted: boolean | null;
  raw: { kind: string; key: string; path: string } | null;
}

export interface ToolCall {
  tool: string;
  input: string;
  output: string;
}

export interface Rollout {
  id: string;
  version: number;
  slot: number;
  retry: number;
  status: JobStatus;
  outcome: Outcome | null;
  reward: number | null;
  error: string | null;
  trace_uri: string | null;
  trace_metrics: {
    llm_calls?: number;
    input_tokens?: number | null;
    output_tokens?: number | null;
    duration_seconds?: number | null;
    declared_complete?: boolean;
    files_modified?: string[] | null;
    tool_calls?: ToolCall[];
  } | null;
  failure: {
    failing_tests?: string[];
    explanation?: string | null;
    attribution?: string;
    pitfall_matched?: string | null;
  } | null;
  cost_usd: number | null;
}

export interface TaskDetail {
  task: {
    id: string;
    name: string | null;
    slot: Record<string, unknown>;
    status: TaskStatus;
    reason: string | null;
    current_version: number;
    stage: string;
    accepted_version: number | null;
    batch_id: string | null;
    created_at: string;
    updated_at: string;
  };
  versions: Version[];
  rollouts: Rollout[];
  jobs: Job[];
  decision: string;
  reason: string | null;
  cost_usd: number | null;
  dev: boolean;
}

export interface ArtifactListing {
  kind: string;
  key: string;
  total: number;
  files: { path: string; size: number }[];
}

export interface ArtifactFile {
  path: string;
  size: number;
  binary: boolean;
  truncated: boolean;
  text: string | null;
}
