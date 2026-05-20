SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    auto_reason INTEGER NOT NULL DEFAULT 0,
    allowed_auto_workers_json TEXT,
    default_timeout_seconds INTEGER,
    default_conclude_timeout_seconds INTEGER,
    environment_id TEXT,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT,
    description TEXT NOT NULL,
    metadata_json TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    requested_worker TEXT,
    timeout_override_seconds INTEGER,
    conclude_timeout_override_seconds INTEGER,
    control_state TEXT NOT NULL DEFAULT 'normal',
    control_requested_at TEXT,
    control_requested_by TEXT,
    control_reason TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS work_environments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    backend TEXT NOT NULL,
    ssh_command TEXT,
    workspace_root TEXT,
    cleanup_json TEXT,
    terminal_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_health_status TEXT,
    last_healthcheck_json TEXT
);

CREATE TABLE IF NOT EXISTS environment_provider_endpoints (
    environment_id TEXT NOT NULL REFERENCES work_environments(id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL,
    type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    provider_api TEXT,
    api_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (environment_id, endpoint_id)
);

CREATE TABLE IF NOT EXISTS worker_inventory (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    model_profile TEXT,
    model TEXT,
    model_context_window INTEGER,
    endpoint TEXT,
    task_types_json TEXT NOT NULL,
    max_running INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    allowed_environments_json TEXT,
    question_capability_json TEXT,
    capability_updated_at TEXT,
    capability_source TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_runtime_health (
    environment_id TEXT NOT NULL REFERENCES work_environments(id) ON DELETE CASCADE,
    worker_name TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    endpoint_id TEXT NOT NULL DEFAULT '',
    model_profile_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (status IN ('ok', 'unhealthy', 'unknown')),
    checked_at TEXT NOT NULL,
    stale_after TEXT,
    disabled_until TEXT,
    source TEXT,
    dispatcher_id TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (environment_id, worker_name, worker_type, endpoint_id, model_profile_id)
);

CREATE INDEX IF NOT EXISTS idx_worker_runtime_health_worker
ON worker_runtime_health (worker_name, worker_type, endpoint_id, model_profile_id);

CREATE TABLE IF NOT EXISTS question_threads (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    anchor_type TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    source_run_log_id TEXT,
    source_remote_session_kind TEXT,
    source_remote_session_id TEXT,
    source_remote_session_status TEXT NOT NULL,
    worker_name TEXT,
    execution_environment_id TEXT,
    execution_worker_type TEXT,
    execution_endpoint_id TEXT,
    execution_model_profile_id TEXT,
    mode TEXT NOT NULL,
    session_effect TEXT NOT NULL,
    status TEXT NOT NULL,
    notice TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS question_jobs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES question_threads(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    mode TEXT NOT NULL,
    message TEXT NOT NULL,
    prompt_context_json TEXT,
    status TEXT NOT NULL,
    execution_environment_id TEXT,
    execution_worker_type TEXT,
    execution_endpoint_id TEXT,
    execution_model_profile_id TEXT,
    claimed_by TEXT,
    claimed_at TEXT,
    claim_expires_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    result_text TEXT,
    error_code TEXT,
    error_detail TEXT,
    run_log_id TEXT,
    question_remote_session_kind TEXT,
    question_remote_session_id TEXT,
    question_remote_session_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(thread_id, seq)
);

CREATE TABLE IF NOT EXISTS question_events (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES question_threads(id) ON DELETE CASCADE,
    job_id TEXT REFERENCES question_jobs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_key TEXT,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS question_resume_locks (
    project_id TEXT NOT NULL,
    remote_session_kind TEXT NOT NULL,
    remote_session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    job_id TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, remote_session_kind, remote_session_id)
);

CREATE INDEX IF NOT EXISTS idx_question_jobs_status_claim
ON question_jobs (status, claim_expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_question_jobs_project_thread_seq
ON question_jobs (project_id, thread_id, seq);

CREATE INDEX IF NOT EXISTS idx_question_events_thread_seq
ON question_events (thread_id, seq);

CREATE UNIQUE INDEX IF NOT EXISTS idx_question_events_job_key
ON question_events (job_id, event_key) WHERE event_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS run_provenance (
    run_log_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT,
    task_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    worker_name TEXT NOT NULL,
    worker_type TEXT,
    environment_id TEXT,
    environment_backend TEXT,
    environment_target TEXT,
    workspace TEXT,
    model_profile_id TEXT,
    endpoint_id TEXT,
    timeout_seconds INTEGER,
    report_path TEXT,
    report_run_id TEXT,
    remote_session_id TEXT,
    remote_session_kind TEXT,
    remote_session_status TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (remote_session_status IN ('available', 'missing', 'unresolved')),
    remote_session_capture_method TEXT,
    parent_run_log_id TEXT,
    parent_remote_session_id TEXT,
    question_mode TEXT,
    question_anchor_type TEXT,
    question_anchor_id TEXT,
    source_run_log_id TEXT,
    source_remote_session_id TEXT,
    session_effect TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    returncode INTEGER,
    timed_out INTEGER,
    cancelled INTEGER,
    cancel_reason TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_provenance_project_intent_started
ON run_provenance (project_id, intent_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_run_provenance_project_task_started
ON run_provenance (project_id, task_type, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_run_provenance_project_source_run
ON run_provenance (project_id, source_run_log_id);

CREATE INDEX IF NOT EXISTS idx_run_provenance_remote_session
ON run_provenance (remote_session_kind, remote_session_id);

INSERT OR IGNORE INTO work_environments (
    id, label, backend, workspace_root, cleanup_json, terminal_json, created_at, updated_at
) VALUES (
    'docker-default', 'Docker Default', 'docker', NULL, '{"completed_action":"stop"}', '{"mode":"none"}', strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now')
);
"""
