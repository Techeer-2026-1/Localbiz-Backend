-- users v1→v2 마이그레이션 (FK 우회 패턴, ERD v6.3 §6 정합)
BEGIN;

-- FK 제거 (의존 테이블 보존)
ALTER TABLE user_favorites DROP CONSTRAINT IF EXISTS user_favorites_user_id_fkey;
ALTER TABLE reviews        DROP CONSTRAINT IF EXISTS reviews_user_id_fkey;
ALTER TABLE conversations  DROP CONSTRAINT IF EXISTS conversations_user_id_fkey;

-- 의존 컬럼 UUID → BIGINT (data 0 row이므로 USING NULL 안전)
ALTER TABLE user_favorites ALTER COLUMN user_id TYPE BIGINT USING NULL::bigint;
ALTER TABLE reviews        ALTER COLUMN user_id TYPE BIGINT USING NULL::bigint;
ALTER TABLE conversations  ALTER COLUMN user_id TYPE BIGINT USING NULL::bigint;

-- users v1 DROP + v2 CREATE (ERD v6.3 §6 + 19 불변식 #15)
DROP TABLE users;
CREATE TABLE users (
    user_id          BIGSERIAL PRIMARY KEY,
    email            VARCHAR(200) NOT NULL UNIQUE,
    password_hash    VARCHAR(200),
    auth_provider    VARCHAR(20) NOT NULL DEFAULT 'email',
    google_id        VARCHAR(100) UNIQUE,
    nickname         VARCHAR(100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted       BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT users_auth_provider_chk CHECK (auth_provider IN ('email','google')),
    CONSTRAINT users_email_or_google_chk CHECK (
        (auth_provider='email'  AND password_hash IS NOT NULL AND google_id IS NULL) OR
        (auth_provider='google' AND password_hash IS NULL     AND google_id IS NOT NULL)
    )
);
CREATE INDEX users_email_idx ON users(email) WHERE is_deleted = FALSE;

-- FK 재설정 (운영 ERD 정합)
ALTER TABLE user_favorites ADD CONSTRAINT user_favorites_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
ALTER TABLE reviews        ADD CONSTRAINT reviews_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE conversations  ADD CONSTRAINT conversations_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;

COMMIT;
