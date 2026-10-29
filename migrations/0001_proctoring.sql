-- 0001_proctoring.sql  — idempotent, re-runnable, ADDITIVE ONLY. DB: hr_portal
--
-- This migration adds the columns + indexes the proctoring module needs. It is safe to
-- run multiple times and NEVER drops or recreates a table. Every column/index add uses the
-- guarded information_schema dynamic-SQL pattern so it runs on BOTH Oracle MySQL 8.0 and
-- MariaDB (neither `ADD COLUMN IF NOT EXISTS` nor `ADD INDEX IF NOT EXISTS` is portable —
-- `IF NOT EXISTS` on columns is a MariaDB-only extension and raises ER_PARSE_ERROR on
-- stock Oracle MySQL 8.0).
--
-- DEPLOY ORDER (R3): apply this file BEFORE deploying the updated Main.py, because
-- SQLAlchemy's db.create_all() never ALTERs existing tables.
--   mysql -u root -p hr_portal < migrations/0001_proctoring.sql
--
-- occurred_at already exists on proctoring_events (DATETIME). New raw writes use NOW() to
-- match started_at's clock (see Main.py), so no change to occurred_at is required here.

-- ============================================================
-- proctoring_events columns
-- ============================================================

-- proctoring_events.incident_id
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND column_name='incident_id');
SET @s := IF(@c=0,'ALTER TABLE proctoring_events ADD COLUMN incident_id VARCHAR(64) NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- proctoring_events.duration_ms
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND column_name='duration_ms');
SET @s := IF(@c=0,'ALTER TABLE proctoring_events ADD COLUMN duration_ms INT NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- proctoring_events.confidence (NULL by default — only device_detected carries a real 0-1 score)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND column_name='confidence');
SET @s := IF(@c=0,'ALTER TABLE proctoring_events ADD COLUMN confidence FLOAT NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- proctoring_events.detection_source  client|server|model
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND column_name='detection_source');
SET @s := IF(@c=0,"ALTER TABLE proctoring_events ADD COLUMN detection_source VARCHAR(16) NOT NULL DEFAULT 'client'",'SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- proctoring_events.review_status  pending|confirmed|dismissed
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND column_name='review_status');
SET @s := IF(@c=0,"ALTER TABLE proctoring_events ADD COLUMN review_status VARCHAR(16) NOT NULL DEFAULT 'pending'",'SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- proctoring_events.admin_notes
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND column_name='admin_notes');
SET @s := IF(@c=0,'ALTER TABLE proctoring_events ADD COLUMN admin_notes TEXT NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- ============================================================
-- assessment_attempts columns
-- ============================================================

-- assessment_attempts.identity_verified  (SMALLINT to match ORM db.SmallInteger — no TINYINT/SMALLINT drift)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='identity_verified');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN identity_verified SMALLINT NOT NULL DEFAULT 0','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- assessment_attempts.identity_snapshot  base64 data-URL JPEG (LONGTEXT — matches existing snapshot_b64 TEXT pattern)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='identity_snapshot');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN identity_snapshot LONGTEXT NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- assessment_attempts.precheck_report  JSON (raw path must json.dumps before insert)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='precheck_report');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN precheck_report JSON NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- assessment_attempts.last_heartbeat
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='last_heartbeat');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN last_heartbeat DATETIME NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- assessment_attempts.draft_answers  JSON (R1 — MCQ autosave partial answers)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='draft_answers');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN draft_answers JSON NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- ============================================================
-- guarded index creation (same pattern on information_schema.statistics)
-- No FK on incident_id — it is a server-minted logical id, not a row reference.
-- ============================================================

SET @i := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND index_name='ix_pe_incident');
SET @s := IF(@i=0,'CREATE INDEX ix_pe_incident ON proctoring_events (incident_id)','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @j := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND index_name='ix_pe_attempt_incident');
SET @s := IF(@j=0,'CREATE INDEX ix_pe_attempt_incident ON proctoring_events (attempt_id, incident_id)','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;
