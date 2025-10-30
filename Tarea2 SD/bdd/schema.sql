CREATE TABLE IF NOT EXISTS question (
  id BIGSERIAL PRIMARY KEY,
  question_hash TEXT UNIQUE NOT NULL,
  class INT,
  title TEXT,
  content TEXT,
  best_answer TEXT
);

CREATE TABLE IF NOT EXISTS response_event (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  question_hash TEXT NOT NULL REFERENCES question(question_hash),
  llm_answer TEXT,
  score INT,
  hit BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS question_counter (
  question_hash TEXT PRIMARY KEY REFERENCES question(question_hash),
  times_asked BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_response_event_qhash_ts
  ON response_event (question_hash, ts DESC);

CREATE INDEX IF NOT EXISTS ix_question_class
  ON question (class);

