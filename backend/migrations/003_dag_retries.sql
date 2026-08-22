ALTER TABLE agent_run_steps
    ADD COLUMN depends_on text[] NOT NULL DEFAULT '{}',
    ADD COLUMN max_retries smallint NOT NULL DEFAULT 0 CHECK (max_retries BETWEEN 0 AND 5),
    ADD COLUMN attempts smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0);

CREATE INDEX agent_run_steps_dependency_idx
    ON agent_run_steps (tenant_id, run_id, position);
