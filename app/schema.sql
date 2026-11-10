CREATE TABLE IF NOT EXISTS classes (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    age_group TEXT NOT NULL CHECK (age_group IN ('1-4', '5-8', '9-11')),
    invite_code TEXT NOT NULL UNIQUE,
    points INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE classes
    ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    max_user_id BIGINT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'student'
        CHECK (role IN ('student', 'teacher', 'moderator', 'admin')),
    class_id BIGINT REFERENCES classes(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS teacher_invites (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    class_id BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'used', 'revoked')),
    used_by_max_user_id BIGINT,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS teacher_applications (
    id BIGSERIAL PRIMARY KEY,
    max_user_id BIGINT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    invite_id BIGINT NOT NULL REFERENCES teacher_invites(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    moderator_comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ideas (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    target_group TEXT NOT NULL,
    author_class_id BIGINT NOT NULL REFERENCES classes(id),
    author_user_id BIGINT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'needs_revision')),
    moderator_comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE ideas ADD COLUMN IF NOT EXISTS author_user_id BIGINT;

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    event_date DATE NOT NULL,
    author_class_id BIGINT NOT NULL REFERENCES classes(id),
    status TEXT NOT NULL DEFAULT 'approved'
        CHECK (status IN ('draft', 'approved', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS event_signups (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    class_id BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, class_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id),
    class_id BIGINT NOT NULL REFERENCES classes(id),
    submitted_by_max_user_id BIGINT,
    media_token TEXT,
    comment TEXT,
    moderator_comment TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    checked_by BIGINT REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checked_at TIMESTAMPTZ,
    CHECK (media_token IS NOT NULL OR comment IS NOT NULL)
);

ALTER TABLE reports ADD COLUMN IF NOT EXISTS submitted_by_max_user_id BIGINT;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS moderator_comment TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS reports_event_user_unique
    ON reports (event_id, submitted_by_max_user_id)
    WHERE submitted_by_max_user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS reports_event_class_unique
    ON reports (event_id, class_id)
    WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS points_log (
    id BIGSERIAL PRIMARY KEY,
    class_id BIGINT NOT NULL REFERENCES classes(id),
    reason TEXT NOT NULL,
    points INTEGER NOT NULL,
    period_month DATE,
    confirmed_by BIGINT REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE points_log ADD COLUMN IF NOT EXISTS period_month DATE;
UPDATE points_log
SET period_month = date_trunc('month', created_at AT TIME ZONE 'Europe/Moscow')::date
WHERE period_month IS NULL;
ALTER TABLE points_log
    ALTER COLUMN period_month SET DEFAULT (date_trunc(
        'month', timezone('Europe/Moscow', NOW())
    )::date);
ALTER TABLE points_log ALTER COLUMN period_month SET NOT NULL;

CREATE TABLE IF NOT EXISTS rating_periods (
    id BIGSERIAL PRIMARY KEY,
    period_month DATE NOT NULL UNIQUE,
    monthly_value TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed')),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    CHECK (period_month = (date_trunc('month', period_month)::date))
);

CREATE TABLE IF NOT EXISTS rating_winners (
    id BIGSERIAL PRIMARY KEY,
    period_id BIGINT NOT NULL REFERENCES rating_periods(id) ON DELETE CASCADE,
    class_id BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    points INTEGER NOT NULL CHECK (points >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (period_id, class_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO settings (key, value)
VALUES
    ('monthly_value', '"Взаимопомощь"'),
    ('monthly_approval_limit', '5'),
    ('class_idea_limit', '3'),
    ('points_scale', '{"participation": 5, "approved_idea": 10, "prize_place": 15}')
ON CONFLICT (key) DO NOTHING;

