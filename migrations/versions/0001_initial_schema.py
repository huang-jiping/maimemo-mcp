"""Initial PostgreSQL schema, frozen independently of application ORM metadata."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_STATEMENTS = (
    """CREATE TABLE api_rate_limit_window (
    token_hash VARCHAR NOT NULL,
    window_type VARCHAR NOT NULL,
    window_start TIMESTAMP WITH TIME ZONE NOT NULL,
    used_requests INTEGER DEFAULT '0' NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    id UUID NOT NULL,
    CONSTRAINT pk_api_rate_limit_window PRIMARY KEY (id),
    CONSTRAINT uq_api_rate_limit_window_identity UNIQUE (token_hash, window_type, window_start),
    CONSTRAINT ck_api_rate_limit_window_token_hash CHECK (length(token_hash) = 64),
    CONSTRAINT ck_api_rate_limit_window_window_type CHECK (window_type IN ('10s', '60s', '5h')),
    CONSTRAINT ck_api_rate_limit_window_used_requests CHECK (used_requests >= 0)
)""",
    """CREATE TABLE daily_progress (
    study_date DATE NOT NULL,
    completed_count INTEGER,
    total_count INTEGER,
    study_seconds INTEGER,
    completeness VARCHAR NOT NULL,
    id UUID NOT NULL,
    first_observed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    last_observed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_daily_progress PRIMARY KEY (id),
    CONSTRAINT ck_daily_progress_counts CHECK (completed_count >= 0 AND total_count >= 0
    AND total_count >= completed_count AND study_seconds >= 0),
    CONSTRAINT ck_daily_progress_completeness CHECK (completeness IN ('complete', 'partial',
    'stale', 'unavailable')),
    CONSTRAINT ck_daily_progress_time_order CHECK (last_observed_at >= first_observed_at),
    CONSTRAINT uq_daily_progress_study_date UNIQUE (study_date)
)""",
    """CREATE TABLE ingestion_run (
    task_type VARCHAR NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR NOT NULL,
    request_count INTEGER DEFAULT '0' NOT NULL,
    result_count INTEGER DEFAULT '0' NOT NULL,
    error_category VARCHAR,
    error_summary VARCHAR,
    id UUID NOT NULL,
    CONSTRAINT pk_ingestion_run PRIMARY KEY (id),
    CONSTRAINT ck_ingestion_run_status CHECK (status IN
    ('running', 'complete', 'partial', 'failed')),
    CONSTRAINT ck_ingestion_run_counts CHECK (request_count >= 0 AND result_count >= 0),
    CONSTRAINT ck_ingestion_run_time_order CHECK (finished_at IS NULL OR finished_at >= started_at)
)""",
    """CREATE TABLE schema_metadata (
    schema_version VARCHAR NOT NULL,
    installed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    id UUID NOT NULL,
    CONSTRAINT pk_schema_metadata PRIMARY KEY (id),
    CONSTRAINT uq_schema_metadata_schema_version UNIQUE (schema_version)
)""",
    """CREATE TABLE vocabulary (
    maimemo_id BIGINT NOT NULL,
    normalized_spelling VARCHAR NOT NULL,
    spelling VARCHAR NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    id UUID NOT NULL,
    CONSTRAINT pk_vocabulary PRIMARY KEY (id),
    CONSTRAINT ck_vocabulary_maimemo_id CHECK (maimemo_id > 0),
    CONSTRAINT ck_vocabulary_spelling CHECK (length(normalized_spelling) > 0 AND
    length(spelling) > 0),
    CONSTRAINT ck_vocabulary_time_order CHECK (last_seen_at >= first_seen_at),
    CONSTRAINT uq_vocabulary_maimemo_id UNIQUE (maimemo_id)
)""",
    """CREATE TABLE api_snapshot (
    endpoint VARCHAR NOT NULL,
    request_hash VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    raw_response JSONB NOT NULL,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    ingestion_run_id UUID NOT NULL,
    id UUID NOT NULL,
    CONSTRAINT pk_api_snapshot PRIMARY KEY (id),
    CONSTRAINT uq_api_snapshot_content UNIQUE (endpoint, request_hash, content_hash),
    CONSTRAINT ck_api_snapshot_endpoint CHECK (length(endpoint) > 0),
    CONSTRAINT ck_api_snapshot_hashes CHECK (length(request_hash) > 0 AND length(content_hash) > 0),
    CONSTRAINT fk_api_snapshot_ingestion_run_id_ingestion_run FOREIGN KEY(ingestion_run_id)
    REFERENCES ingestion_run (id)
)""",
    """CREATE TABLE learning_feedback_event (
    event_type VARCHAR NOT NULL,
    word_a_id UUID,
    word_b_id UUID,
    word_a_spelling VARCHAR,
    word_b_spelling VARCHAR,
    word_a_status VARCHAR DEFAULT 'UNRESOLVED' NOT NULL,
    word_b_status VARCHAR DEFAULT 'UNRESOLVED' NOT NULL,
    relation_type VARCHAR,
    direction VARCHAR,
    evidence_type VARCHAR,
    note VARCHAR,
    source_agent VARCHAR,
    session_reference VARCHAR,
    idempotency_key VARCHAR NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    retracted_event_id UUID,
    id UUID NOT NULL,
    CONSTRAINT pk_learning_feedback_event PRIMARY KEY (id),
    CONSTRAINT ck_learning_feedback_event_event_type CHECK (event_type IN ('CONFUSION',
    'RETRACTION')),
    CONSTRAINT ck_learning_feedback_event_evidence_type CHECK (evidence_type IN
    ('USER_CONFIRMED', 'QUIZ_OBSERVED')),
    CONSTRAINT ck_learning_feedback_event_direction CHECK (direction IN ('A_TO_B', 'B_TO_A',
    'BIDIRECTIONAL')),
    CONSTRAINT ck_learning_feedback_event_word_a_status CHECK (word_a_status IN ('RESOLVED',
    'UNRESOLVED')),
    CONSTRAINT ck_learning_feedback_event_word_b_status CHECK (word_b_status IN ('RESOLVED',
    'UNRESOLVED')),
    CONSTRAINT ck_learning_feedback_event_idempotency_key CHECK (length(idempotency_key) > 0),
    CONSTRAINT ck_learning_feedback_event_event_shape CHECK ((event_type = 'RETRACTION' AND
    retracted_event_id IS NOT NULL AND retracted_event_id != id) OR (event_type = 'CONFUSION'
    AND retracted_event_id IS NULL AND relation_type IS NOT NULL AND length(relation_type) > 0
    AND direction IS NOT NULL AND evidence_type IS NOT NULL AND source_agent IS NOT NULL AND
    length(source_agent) > 0 AND ((word_a_status = 'RESOLVED' AND word_a_id IS NOT NULL) OR
    (word_a_status = 'UNRESOLVED' AND word_a_id IS NULL AND word_a_spelling IS NOT NULL AND
    length(word_a_spelling) > 0)) AND ((word_b_status = 'RESOLVED' AND word_b_id IS NOT NULL)
    OR (word_b_status = 'UNRESOLVED' AND word_b_id IS NULL AND word_b_spelling IS NOT NULL AND
    length(word_b_spelling) > 0)))),
    CONSTRAINT fk_learning_feedback_event_word_a_id_vocabulary FOREIGN KEY(word_a_id)
    REFERENCES vocabulary (id),
    CONSTRAINT fk_learning_feedback_event_word_b_id_vocabulary FOREIGN KEY(word_b_id)
    REFERENCES vocabulary (id),
    CONSTRAINT uq_learning_feedback_event_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT fk_learning_feedback_event_retracted_event_id_learning__4c08 FOREIGN
    KEY(retracted_event_id) REFERENCES learning_feedback_event (id)
)""",
    """CREATE TABLE daily_word_observation (
    study_date DATE NOT NULL,
    vocabulary_id UUID NOT NULL,
    first_feedback VARCHAR,
    is_new BOOLEAN,
    is_complete BOOLEAN,
    source_snapshot_id UUID NOT NULL,
    id UUID NOT NULL,
    first_observed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    last_observed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_daily_word_observation PRIMARY KEY (id),
    CONSTRAINT uq_daily_word_observation_day_word UNIQUE (study_date, vocabulary_id),
    CONSTRAINT ck_daily_word_observation_feedback CHECK (first_feedback IS NULL OR
    length(first_feedback) > 0),
    CONSTRAINT ck_daily_word_observation_time_order CHECK (last_observed_at >= first_observed_at),
    CONSTRAINT fk_daily_word_observation_vocabulary_id_vocabulary FOREIGN KEY(vocabulary_id)
    REFERENCES vocabulary (id),
    CONSTRAINT fk_daily_word_observation_source_snapshot_id_api_snapshot FOREIGN
    KEY(source_snapshot_id) REFERENCES api_snapshot (id)
)""",
    """CREATE TABLE study_record_snapshot (
    vocabulary_id UUID NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    added_at TIMESTAMP WITH TIME ZONE,
    first_studied_at TIMESTAMP WITH TIME ZONE,
    last_studied_at TIMESTAMP WITH TIME ZONE,
    next_study_at TIMESTAMP WITH TIME ZONE,
    last_feedback VARCHAR,
    study_count INTEGER,
    tags JSONB,
    source_snapshot_id UUID NOT NULL,
    id UUID NOT NULL,
    CONSTRAINT pk_study_record_snapshot PRIMARY KEY (id),
    CONSTRAINT uq_study_record_snapshot_source UNIQUE (vocabulary_id, source_snapshot_id),
    CONSTRAINT ck_study_record_snapshot_study_count CHECK (study_count IS NULL OR study_count >= 0),
    CONSTRAINT ck_study_record_snapshot_feedback CHECK (last_feedback IS NULL OR
    length(last_feedback) > 0),
    CONSTRAINT ck_study_record_snapshot_tags CHECK (jsonb_typeof(tags) = 'array'),
    CONSTRAINT fk_study_record_snapshot_vocabulary_id_vocabulary FOREIGN KEY(vocabulary_id)
    REFERENCES vocabulary (id),
    CONSTRAINT fk_study_record_snapshot_source_snapshot_id_api_snapshot FOREIGN
    KEY(source_snapshot_id) REFERENCES api_snapshot (id)
)""",
    """CREATE TABLE weakness_score (
    vocabulary_id UUID NOT NULL,
    computed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    algorithm_version VARCHAR NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    risk_level VARCHAR NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    factors JSONB NOT NULL,
    evidence_from TIMESTAMP WITH TIME ZONE,
    evidence_through TIMESTAMP WITH TIME ZONE,
    latest_snapshot_id UUID NOT NULL,
    id UUID NOT NULL,
    CONSTRAINT pk_weakness_score PRIMARY KEY (id),
    CONSTRAINT uq_weakness_score_version UNIQUE (vocabulary_id, algorithm_version, computed_at),
    CONSTRAINT ck_weakness_score_score CHECK (score >= 0 AND score <= 100),
    CONSTRAINT ck_weakness_score_confidence CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT ck_weakness_score_risk_level CHECK (risk_level IN ('low', 'medium', 'high')),
    CONSTRAINT ck_weakness_score_algorithm_version CHECK (length(algorithm_version) > 0),
    CONSTRAINT ck_weakness_score_factors CHECK (jsonb_typeof(factors) = 'object'),
    CONSTRAINT ck_weakness_score_time_order CHECK (evidence_through >= evidence_from),
    CONSTRAINT fk_weakness_score_vocabulary_id_vocabulary FOREIGN KEY(vocabulary_id) REFERENCES
    vocabulary (id),
    CONSTRAINT fk_weakness_score_latest_snapshot_id_api_snapshot FOREIGN
    KEY(latest_snapshot_id) REFERENCES api_snapshot (id)
)""",
    """CREATE INDEX ix_api_rate_limit_window_window_start ON api_rate_limit_window
    (window_start)""",
    """CREATE INDEX ix_ingestion_run_task_started ON ingestion_run (task_type, started_at)""",
    """CREATE INDEX ix_vocabulary_normalized_spelling ON vocabulary (normalized_spelling)""",
    """CREATE INDEX ix_api_snapshot_endpoint_fetched ON api_snapshot (endpoint, fetched_at)""",
    """CREATE INDEX ix_api_snapshot_ingestion_run_id ON api_snapshot (ingestion_run_id)""",
    """CREATE INDEX ix_learning_feedback_event_created_at ON learning_feedback_event
    (created_at)""",
    """CREATE INDEX ix_learning_feedback_event_retracted_event_id ON learning_feedback_event
    (retracted_event_id)""",
    """CREATE INDEX ix_learning_feedback_event_word_a_created ON learning_feedback_event
    (word_a_id, created_at)""",
    """CREATE INDEX ix_learning_feedback_event_word_b_created ON learning_feedback_event
    (word_b_id, created_at)""",
    """CREATE INDEX ix_daily_word_observation_source_snapshot_id ON daily_word_observation
    (source_snapshot_id)""",
    """CREATE INDEX ix_daily_word_observation_word_day ON daily_word_observation
    (vocabulary_id, study_date)""",
    """CREATE INDEX ix_study_record_snapshot_next_study_at ON study_record_snapshot
    (next_study_at)""",
    """CREATE INDEX ix_study_record_snapshot_source_snapshot_id ON study_record_snapshot
    (source_snapshot_id)""",
    """CREATE INDEX ix_study_record_snapshot_word_observed ON study_record_snapshot
    (vocabulary_id, observed_at)""",
    """CREATE INDEX ix_weakness_score_computed_at ON weakness_score (computed_at)""",
    """CREATE INDEX ix_weakness_score_latest_snapshot_id ON weakness_score (latest_snapshot_id)""",
    """CREATE INDEX ix_weakness_score_version_score ON weakness_score (algorithm_version, score)""",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute(
        "INSERT INTO schema_metadata (id, schema_version) "
        "VALUES ('00000000-0000-0000-0000-000000000001', '0001')"
    )


def downgrade() -> None:
    for table in [
        "weakness_score",
        "study_record_snapshot",
        "daily_word_observation",
        "learning_feedback_event",
        "api_snapshot",
        "vocabulary",
        "schema_metadata",
        "ingestion_run",
        "daily_progress",
        "api_rate_limit_window",
    ]:
        op.drop_table(table)
