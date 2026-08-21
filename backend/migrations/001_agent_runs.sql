CREATE TABLE agent_runs (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    project_id text,
    idempotency_key text NOT NULL,
    task text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    summary text NOT NULL,
    cancelled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, id)
);

CREATE INDEX agent_runs_tenant_created_at_idx ON agent_runs (tenant_id, created_at DESC);

CREATE TABLE agent_run_steps (
    run_id uuid NOT NULL,
    tenant_id text NOT NULL,
    position smallint NOT NULL CHECK (position >= 0),
    node_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    output text NOT NULL DEFAULT '',
    error text NOT NULL DEFAULT '',
    latency_ms integer NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    PRIMARY KEY (run_id, position),
    FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX agent_run_steps_tenant_run_idx ON agent_run_steps (tenant_id, run_id, position);

ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_run_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_run_steps FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_runs_tenant_isolation ON agent_runs
    USING (tenant_id = current_setting('app.current_tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));

CREATE POLICY agent_run_steps_tenant_isolation ON agent_run_steps
    USING (tenant_id = current_setting('app.current_tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));
