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

ALTER TABLE classes ADD COLUMN IF NOT EXISTS academic_year TEXT;
UPDATE classes
SET academic_year = CASE
    WHEN EXTRACT(MONTH FROM timezone('Europe/Moscow', NOW()))::int >= 9
        THEN EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int::text || '/' ||
             (EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int + 1)::text
    ELSE (EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int - 1)::text || '/' ||
         EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int::text
END
WHERE academic_year IS NULL;
ALTER TABLE classes ALTER COLUMN academic_year SET NOT NULL;
ALTER TABLE classes DROP CONSTRAINT IF EXISTS classes_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS classes_active_name_unique
    ON classes (name) WHERE archived = FALSE;

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

CREATE TABLE IF NOT EXISTS teacher_class_assignments (
    teacher_max_user_id BIGINT NOT NULL REFERENCES users(max_user_id) ON DELETE CASCADE,
    class_id BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (teacher_max_user_id, class_id)
);

CREATE TABLE IF NOT EXISTS teacher_application_classes (
    application_id BIGINT NOT NULL REFERENCES teacher_applications(id) ON DELETE CASCADE,
    class_id BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    PRIMARY KEY (application_id, class_id)
);

CREATE INDEX IF NOT EXISTS teacher_class_assignments_class_idx
    ON teacher_class_assignments (class_id);
CREATE INDEX IF NOT EXISTS teacher_application_classes_class_idx
    ON teacher_application_classes (class_id);

INSERT INTO teacher_class_assignments (teacher_max_user_id, class_id)
SELECT max_user_id, class_id
FROM users
WHERE role = 'teacher' AND class_id IS NOT NULL
ON CONFLICT DO NOTHING;

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

CREATE TABLE IF NOT EXISTS monthly_class_bonuses (
    id BIGSERIAL PRIMARY KEY,
    class_id BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    period_month DATE NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN ('punctuality', 'attendance', 'discipline', 'tidiness')
    ),
    points INTEGER NOT NULL CHECK (points >= 0),
    points_log_id BIGINT NOT NULL UNIQUE REFERENCES points_log(id) ON DELETE CASCADE,
    updated_by_max_user_id BIGINT REFERENCES users(max_user_id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (class_id, period_month, category),
    CHECK (period_month = date_trunc('month', period_month)::date)
);

CREATE TABLE IF NOT EXISTS discipline_penalties (
    id BIGSERIAL PRIMARY KEY,
    class_id BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    period_month DATE NOT NULL,
    teacher_max_user_id BIGINT NOT NULL REFERENCES users(max_user_id) ON DELETE CASCADE,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (period_month = date_trunc('month', period_month)::date)
);

CREATE INDEX IF NOT EXISTS discipline_penalties_class_month_idx
    ON discipline_penalties (class_id, period_month);

CREATE TABLE IF NOT EXISTS rating_action_audit (
    id BIGSERIAL PRIMARY KEY,
    action_type TEXT NOT NULL CHECK (action_type IN ('bonus', 'penalty')),
    class_id BIGINT REFERENCES classes(id) ON DELETE SET NULL,
    class_name TEXT NOT NULL,
    period_month DATE NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN ('punctuality', 'attendance', 'discipline', 'tidiness')
    ),
    actor_max_user_id BIGINT NOT NULL,
    event_ref_id BIGINT NOT NULL,
    had_bonus BOOLEAN NOT NULL DEFAULT FALSE,
    previous_points INTEGER,
    new_points INTEGER,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reversed_at TIMESTAMPTZ,
    reversed_by_max_user_id BIGINT,
    CHECK (period_month = date_trunc('month', period_month)::date)
);

CREATE INDEX IF NOT EXISTS rating_action_audit_class_period_idx
    ON rating_action_audit (class_id, period_month, category, id DESC);

ALTER TABLE points_log ADD COLUMN IF NOT EXISTS period_month DATE;
-- Old report/idea awards carry their source ID in the reason. Preserve the
-- activity month instead of the date on which the moderator approved it.
UPDATE points_log AS p
SET period_month = date_trunc('month', e.event_date)::date
FROM reports r JOIN events e ON e.id = r.event_id
WHERE p.period_month IS NULL
  AND p.reason = 'Отчёт №' || r.id || ' подтверждён';
UPDATE points_log AS p
SET period_month = date_trunc('month', i.created_at AT TIME ZONE 'Europe/Moscow')::date
FROM ideas i
WHERE p.period_month IS NULL
  AND p.reason = 'Идея №' || i.id || ' прошла модерацию';
UPDATE points_log
SET period_month = date_trunc('month', created_at AT TIME ZONE 'Europe/Moscow')::date
WHERE period_month IS NULL;
ALTER TABLE points_log
    ALTER COLUMN period_month SET DEFAULT (date_trunc(
        'month', timezone('Europe/Moscow', NOW())
    )::date);
ALTER TABLE points_log ALTER COLUMN period_month SET NOT NULL;
CREATE INDEX IF NOT EXISTS points_log_month_class_idx ON points_log (period_month, class_id);

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

-- Keep earlier activity visible in history without inventing monthly values.
INSERT INTO rating_periods (period_month, monthly_value)
SELECT DISTINCT period_month, 'Ценность не указана (до обновления)'
FROM points_log
WHERE period_month < date_trunc('month', timezone('Europe/Moscow', NOW()))::date
  AND EXTRACT(MONTH FROM period_month) NOT IN (6, 7, 8)
ON CONFLICT (period_month) DO NOTHING;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO settings (key, value)
VALUES
    ('monthly_value', '"Взаимопомощь"'),
    ('active_school_year', to_jsonb(
        CASE
            WHEN EXTRACT(MONTH FROM timezone('Europe/Moscow', NOW()))::int >= 9
                THEN EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int::text || '/' ||
                     (EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int + 1)::text
            ELSE (EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int - 1)::text || '/' ||
                 EXTRACT(YEAR FROM timezone('Europe/Moscow', NOW()))::int::text
        END
    )),
    ('monthly_approval_limit', '5'),
    ('class_idea_limit', '3'),
    ('points_scale', '{"participation": 5, "approved_idea": 10, "prize_place": 15}')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS school_year_rollovers (
    from_year TEXT PRIMARY KEY,
    to_year TEXT NOT NULL,
    performed_by BIGINT NOT NULL,
    promoted_classes INTEGER NOT NULL,
    graduated_classes INTEGER NOT NULL,
    moved_users INTEGER NOT NULL,
    unassigned_users INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_deadline_reminders (
    reminder_date DATE NOT NULL,
    admin_max_user_id BIGINT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (reminder_date, admin_max_user_id)
);

