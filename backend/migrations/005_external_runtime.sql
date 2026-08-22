ALTER TABLE agent_runs
    ADD COLUMN runtime_run_id text;

CREATE UNIQUE INDEX agent_runs_runtime_run_idx
    ON agent_runs (tenant_id, runtime_run_id)
    WHERE runtime_run_id IS NOT NULL;

ALTER TABLE agent_run_steps
    ADD COLUMN prompt text NOT NULL DEFAULT '',
    ADD COLUMN role text NOT NULL DEFAULT '';
