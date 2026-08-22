ALTER TABLE agent_run_steps
    ADD COLUMN model text NOT NULL DEFAULT '',
    ADD COLUMN max_tokens integer NOT NULL DEFAULT 0 CHECK (max_tokens >= 0);
