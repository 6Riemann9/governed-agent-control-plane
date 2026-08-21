ALTER TABLE agent_run_steps
    ADD COLUMN input_tokens integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    ADD COLUMN output_tokens integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0);
