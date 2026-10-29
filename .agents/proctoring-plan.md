# Implementation Plan — Live-Exam Proctoring Module (in-place)

This plan implements the APPROVED, LOCKED design in `.agents/proctoring-design.md` (including the final
`FINAL RESOLUTIONS` R1–R4). It does not re-decide architecture. Every item is grounded in the real source
(`Main.py`, `templates/assessment_take.html`, `templates/hr_assessments.html`) read during planning. Items are
ordered by dependency and each leaves the codebase runnable.

Verified source anchors (line numbers approximate, drift-tolerant):
- Models: `CandidateUser` ~L337 (`passport_photo` LargeBinary + `passport_photo_filename`), `AssessmentAttempt` ~L414
  (`candidate_email`, `warning_count`, `first_warning_reason`, `termination_reason`, `terminated_at`, `started_at`,
  `status`, `video_path`, `reviewed_by_admin`, `requisition_id`), `ProctoringEvent` ~L451
  (`id, attempt_id, event_type(50), severity, description(255), snapshot_b64 TEXT, occurred_at`).
- `get_db_connection()` ~L105 (PyMySQL `DictCursor`, `charset='utf8mb4'`, `autocommit=True`).
- Decorators: `login_required_with_session` ~L71, `role_required(allowed_roles)` ~L81, `HR_ROLES` ~L56.
- Candidate routes: `take_assessment` ~L1062 (`@login_required` + `@login_required_with_session`), `/violation`
  ~L1123, `/whiteboard` ~L1155, `/submit` ~L1175 (API routes carry only `@login_required` today — the IDOR gap).
- `started_at` written with MySQL `NOW()` in start route; `/submit` writes `submitted_at=NOW()`,
  `terminated_at=IF(%s,NOW(),…)` (~L1240s). `assessments.duration_minutes` exists (~L390).
- HR: `/api/hr/attempt/<id>` ~L1396 (`@role_required(HR_ROLES)`), does `SELECT * FROM proctoring_events` (~L1436);
  review modal renders events in `hr_assessments.html` (`openAttempt` ~L483, event map ~L517).
- Client: `CFG` literal ~L323 (dead `noiseDb:26` ~L328), calibration `CFG.noiseDb = Math.max(18, noiseBaseline+14)`
  ~L1064, `IS_HANDHELD` ~L311, `trigger(type,severity,desc)` ~L1462 (POST `{event_type,severity,description,snapshot_b64}`
  to `/violation`), `collectAnswers()` ~L1355, `.opt` click handler ~L1345, `/precheck` POST ~L1902, `API` const ~L293.
- No `migrations/` directory exists yet; no Alembic/Flask-Migrate. Migration is a hand-run `.sql`.

Verification note: the project has no automated test suite and no CI config in the repo. Primary verification is
Python import/compile of `Main.py`, a Jinja render smoke-check of the templates, SQL re-run idempotency, and manual
route smoke tests. Where "run the app" is listed, use the existing entrypoint (`python Main.py`) against the
`hr_portal` MySQL DB. The migration MUST be applied before running the updated `Main.py` (R3).

---

## Section 1 — DB migration + SQLAlchemy model mirror

- [ ] 1. Create the idempotent, additive, flavor-portable migration `migrations/0001_proctoring.sql`.
      Create the `migrations/` directory and the file. Transcribe the guarded `information_schema` dynamic-SQL
      blocks from Decision 2 verbatim — one `SET @c := (SELECT COUNT(*) FROM information_schema.columns …); SET @s := IF(@c=0,'ALTER TABLE … ADD COLUMN …','SELECT 1'); PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;`
      block per column. NEVER use `ADD COLUMN IF NOT EXISTS` (MariaDB-only; parse-errors on Oracle MySQL 8). Never
      drop/recreate tables. Columns to add:
      `proctoring_events`: `incident_id VARCHAR(64) NULL`, `duration_ms INT NULL`, `confidence FLOAT NULL`,
      `detection_source VARCHAR(16) NOT NULL DEFAULT 'client'`, `review_status VARCHAR(16) NOT NULL DEFAULT 'pending'`,
      `admin_notes TEXT NULL`.
      `assessment_attempts`: `identity_verified SMALLINT NOT NULL DEFAULT 0`, `identity_snapshot LONGTEXT NULL`,
      `precheck_report JSON NULL`, `last_heartbeat DATETIME NULL`, `draft_answers JSON NULL` (R1).
      Then guarded `information_schema.statistics` index blocks for `ix_pe_incident (incident_id)` and
      `ix_pe_attempt_incident (attempt_id, incident_id)`. No FK on `incident_id`. Add a top comment stating the file
      is additive/idempotent/re-runnable and must be applied before deploying the updated `Main.py`.
      Files: `migrations/0001_proctoring.sql`
      Verify: apply to the DB (`mysql -u root -p hr_portal < migrations/0001_proctoring.sql`), then run it a SECOND
      time — both runs succeed with no error (idempotency). Confirm with `SHOW COLUMNS FROM proctoring_events;` and
      `SHOW COLUMNS FROM assessment_attempts;` that every new column is present exactly once with the stated type;
      confirm `SHOW INDEX FROM proctoring_events;` lists `ix_pe_incident` and `ix_pe_attempt_incident`.

- [ ] 2. Mirror the new columns in the SQLAlchemy models in `Main.py` so ORM stays consistent with the DB.
      In `ProctoringEvent` (~L451) add: `incident_id = db.Column(db.String(64), index=True)`,
      `duration_ms = db.Column(db.Integer)`, `confidence = db.Column(db.Float)`,
      `detection_source = db.Column(db.String(16), default='client')`,
      `review_status = db.Column(db.String(16), default='pending')`, `admin_notes = db.Column(db.Text)`.
      In `AssessmentAttempt` (~L414) add: `identity_verified = db.Column(db.SmallInteger, default=0)`,
      `identity_snapshot = db.Column(db.Text)` (maps to LONGTEXT), `precheck_report = db.Column(db.JSON)`,
      `last_heartbeat = db.Column(db.DateTime)`, `draft_answers = db.Column(db.JSON)`. Plain column adds only — do
      not touch `__tablename__` or recreate tables. `identity_verified` is `db.SmallInteger` to match the SQL
      `SMALLINT` (no TINYINT/SMALLINT drift).
      Files: `Main.py`
      Verify: `python -c "import Main"` (or `python -m py_compile Main.py`) succeeds with no import/syntax error;
      start the app once and confirm it boots without SQLAlchemy mapper errors.

## Section 2 — Server config, ownership helper, violation engine, new/hardened routes

- [ ] 3. Add `PROCTOR_CFG` + `validate_proctor_cfg()` + shared `SNAPSHOT_MAX = 400000` near the top of `Main.py`
      (after `HR_ROLES`, ~L56). Transcribe the `PROCTOR_CFG` dict from Decision 3 exactly, including
      `noise = {floor_db:18, margin_db:14, sustain_ms:1200, calibration_ms:3000, grace_period_ms:7000}`,
      `warning_cooldown_ms:4500`, the full `max_violations` whitelist whose keys are EXACTLY the client `trigger()`
      event strings (`tab_switch, window_blur, fullscreen_exit, second_display, devtools_open, meta_key, alt_tab,
      capture_shortcut, keyboard_shortcut, clipboard, no_face, multiple_faces, device_detected, eye_away,
      mouth_movement, noise, camera_off, camera_covered, feed_frozen`) plus the new indicator-only emitters
      (`headphone_indicator:-1, earphone_indicator:-1, device_disconnect:-1, network_loss:-1`), the `escalation`
      block (`auto_action:"warn_only"`, `require_human_for_terminate:True`), `evidence`, `network`
      (`heartbeat_ms:15000, server_timeout_s:90`), and `identity` (`require_photo:False, require_match:False`).
      `validate_proctor_cfg()` clamps numeric ranges (durations 200–60000, confidences 0–1, counts >=1), logs a
      warning on out-of-range, and NEVER raises; call it once at import. Define `SNAPSHOT_MAX = 400000` as the single
      module-level constant.
      Files: `Main.py`
      Verify: `python -c "import Main"` succeeds and no validation exception is raised at import; add a quick
      interactive check that `Main.PROCTOR_CFG['max_violations']` keys match the client event set.

- [ ] 4. Add the `_own_attempt(attempt_id)` ownership helper and the non-fatal startup migration presence check (R3).
      `_own_attempt(attempt_id)` loads the attempt row (raw PyMySQL or ORM) and returns it only if
      `candidate_email == current_user.email`, else returns `None` so callers can `return jsonify({'ok':False,'error':'forbidden'}), 403`.
      Add a boot-time check (wrapped in try/except, non-fatal) that queries `information_schema.columns` for the
      sentinel `proctoring_events.incident_id`; if absent, `print()` a loud clear warning that
      `migrations/0001_proctoring.sql` has not been applied. Do not crash the app.
      Files: `Main.py`
      Verify: `python -c "import Main"` succeeds; run the app against a DB WITHOUT the migration and confirm the
      warning prints and the app still boots; run against a migrated DB and confirm no warning.

- [ ] 5. Rework the violation engine. First factor out a pure, DB-free decision function
      `decide_violation_action(event_type, per_type_count, cfg)` returning one of `ignore|noop|warn|auto_submit|terminate`
      per Decision 10 (first warning at `escalation.first_warning_at`; escalate only when count exceeds
      `max_violations[type]`, the type is not `-1`, `auto_action` is `auto_submit`/`terminate`, AND
      (`require_human_for_terminate` is False OR the type is a hard server-verified type); uncertain AI types
      — `headphone_indicator`, `earphone_indicator`, `mouth_movement`, single low-confidence `device_detected` —
      NEVER terminate). Then rewrite `assessment_violation` (~L1123) to: apply the full guard stack
      (`@login_required` + `@login_required_with_session` + `_own_attempt`), require `status=='In Progress'` (else
      `{'action':'ignore'}`), validate/clamp input against the `PROCTOR_CFG['max_violations']` whitelist (unknown
      type -> log with `print()` and treat with a safe default of `{'action':'ignore'}`, NEVER silently drop a real
      event; `severity` enum Low/Medium/High default Medium; `description` <=255; `snapshot_b64` <= `SNAPSHOT_MAX`;
      `confidence` accepted/clamped 0–1 ONLY for `device_detected`, else stored null; `duration_ms` clamp 0–600000;
      `incident_id` <=64, server mints one if absent), de-dup by `incident_id` within `warning_cooldown_ms`
      (extend `duration_ms` on the existing row, return `{'action':'noop'}` instead of inserting a new row), insert
      the event with `detection_source='client'`, maintain a per-type counter, call `decide_violation_action`, set
      `first_warning_reason`/`warning_count` as before, and on `auto_submit`/`terminate` preserve answers and write
      `termination_reason`. Do not swallow DB exceptions silently (log them).
      Files: `Main.py`
      Verify: `python -c "import Main"` succeeds; add/run unit tests for `decide_violation_action` covering warn-only
      default, `-1` indicator never terminates, and count-exceeded escalation only under opt-in `auto_action` (place
      tests under `tests/test_violation_engine.py`, run `python -m pytest tests/test_violation_engine.py`; if pytest
      is unavailable, create a `tests/` module and run it via `python -m unittest`). Manual: POST a duplicate
      `incident_id` within the cooldown and confirm `{'action':'noop'}` with no second row.

- [ ] 6. Harden `assessment_submit` (~L1175): add the full guard stack + `_own_attempt`; add a duplicate-submit guard
      (if `status != 'In Progress'` return the existing result `{'ok':True,'status':<existing>,'duplicate':True}`
      without re-scoring); add timezone-agnostic deadline validation computed entirely in the DB with
      `TIMESTAMPDIFF(SECOND, at.started_at, NOW()) > a.duration_minutes*60` (JOIN `assessments` for
      `duration_minutes`), and if past deadline accept answers but force status per policy and set
      `termination_reason='deadline exceeded'`. Keep `submitted_at=NOW()` / `terminated_at=IF(%s,NOW(),…)` on
      `NOW()` (one clock — do NOT introduce `UTC_TIMESTAMP()`). Fallback to `draft_answers` for any question whose
      submitted answer is missing/empty before scoring (R1); scoring still happens only here. Preserve all answers on
      terminate/auto-submit.
      Files: `Main.py`
      Verify: `python -c "import Main"` succeeds; manual smoke: submit once (status flips to Completed), submit again
      and confirm `duplicate:True` with unchanged score; simulate a past-deadline attempt and confirm `late`/
      `termination_reason='deadline exceeded'` while answers persist.

- [ ] 7. Add the new candidate routes, each with `@login_required` + `@login_required_with_session` + `_own_attempt`
      (R2), reusing `get_db_connection()`:
      `POST /api/assessment/<int:attempt_id>/precheck` — accepts `device_type` (enum, unknown->'unknown'),
      `device_info` (<=255), `noise_baseline` (float clamp 0–120), `passed` (bool), `report` (dict,
      `json.dumps`, reject/truncate >16KB), `identity_snapshot` (optional str, <= `SNAPSHOT_MAX`). Fold
      `device_info`/`passed`/`noise_baseline` into the `precheck_report` JSON (`json.dumps` before the raw insert),
      store `identity_snapshot` verbatim text to `assessment_attempts.identity_snapshot`, set `identity_verified`
      per policy (default 0). Return `{'ok':True}` / `{'ok':False}` 500 on DB error.
      `GET /api/assessment/<int:attempt_id>/registered-photo` — stream the owner's `passport_photo` via `send_file`
      (mirror the `candidate_download` passport branch ~L829); 404 if absent (client falls back to capture-only).
      `POST /api/assessment/<int:attempt_id>/heartbeat` — set `last_heartbeat=NOW()`; return `server_time` and
      `remaining_s` via `GREATEST(a.duration_minutes*60 - TIMESTAMPDIFF(SECOND, at.started_at, NOW()), 0)`; if
      `now - last_heartbeat > PROCTOR_CFG['network']['server_timeout_s']` record a server-side `network_loss` /
      `device_disconnect` event with `detection_source='server'` (R4), never auto-terminating.
      `POST /api/assessment/<int:attempt_id>/autosave` (R1) — require `status=='In Progress'` (else
      `{'ok':False,'ignored':True}` HTTP 200, no write); body `{"answers":{"<qid>":"<A|B|C|D>"}}`; validate keys are
      ints belonging to this attempt's assessment and values in A/B/C/D (ignore anything else); upsert the full
      partial JSON into `draft_answers` (last-write-wins); NO scoring, NO status change.
      ALL server-originated inserts in these routes set `detection_source='server'` explicitly (R4).
      Files: `Main.py`
      Verify: `python -c "import Main"` succeeds; manual smoke each route with an owned attempt (expect 200/stream)
      and with a non-owned attempt (expect 403); confirm `/precheck` persists `precheck_report` as real JSON (not a
      Python `repr`); confirm `/autosave` writes `draft_answers` without changing `status` or `score`.

## Section 3 — Client CFG consolidation + autosave/heartbeat/identity wiring (`assessment_take.html`)

- [ ] 8. Consolidate the client config from the server. In `take_assessment` (`Main.py` ~L1062) pass
      `proctor_cfg=PROCTOR_CFG` to `render_template`. In `assessment_take.html` add a
      `window.PROCTOR_CFG = {{ proctor_cfg|tojson }}` block, then rebuild the existing `CFG` literal (~L323) to
      derive from it while keeping the `IS_HANDHELD` slack as a multiplier (e.g. `eyeAwayMs` from
      `PROCTOR_CFG.gaze_sustain_ms`, `noFaceMs` from `face_visibility_ms`, `detectEveryMs` from `detect_every_ms`,
      etc.). Remove the dead static `noiseDb:26` (~L328) and replace the calibration line (~L1064) with
      `CFG.noiseDb = Math.max(PROCTOR_CFG.noise.floor_db, noiseBaseline + PROCTOR_CFG.noise.margin_db);` and
      `CFG.noiseMs = PROCTOR_CFG.noise.sustain_ms;` so the derived values are byte-for-byte the current live numbers
      (floor 18 / margin 14 / 1200ms) and config stops being forked.
      Files: `templates/assessment_take.html`, `Main.py`
      Verify: load the take page in a browser; `window.PROCTOR_CFG` is populated and `CFG.noiseDb` after calibration
      equals the prior behavior; no JS console errors; the exam still arms and the vision loop runs.

- [ ] 9. Extend the existing `trigger(type, severity, desc)` (~L1462) to mint/track a stable `incident_id` per
      continuous incident (new id when a type fires after a cooldown gap, same id while it re-fires within the
      window — reuse the existing `cd[type]` timing) and add `duration_ms` and `confidence` to the `/violation` POST
      body. Set `confidence` ONLY for `device_detected` (the real COCO `best.score`); every other call sends
      `confidence=null`. For `multiple_faces` keep `confidence=null` and carry the person count in `description`
      ("N persons in frame"). Keep the internal snapshot capture and per-type cooldown unchanged.
      Files: `templates/assessment_take.html`
      Verify: in-browser, trigger a tab-switch and a simulated device detection; confirm the Network tab shows the
      POST body now includes `incident_id`/`duration_ms`/`confidence`, with `confidence` numeric only for
      `device_detected` and null elsewhere; repeated fires within the cooldown reuse the same `incident_id`.

- [ ] 10. Add network + heartbeat + identity wiring. Add `window` `offline`/`online` handlers that show a
      non-blocking banner and fire a client `network_loss` event via `trigger()`; on `online`, re-POST `/autosave`
      and re-read the deadline from the next `/heartbeat` response. Add a `setInterval` posting `/heartbeat` every
      `PROCTOR_CFG.network.heartbeat_ms` and reconcile `remaining_s` against the server clock. In the pre-check flow,
      fetch `/registered-photo` for side-by-side display (capture-only fallback on 404), capture the identity JPEG
      (downscaled <=320px, quality ~0.7), and add `identity_snapshot` to the existing `/precheck` POST body (~L1902).
      Files: `templates/assessment_take.html`
      Verify: in-browser, toggle offline/online (DevTools) and confirm the banner + `network_loss` POST + autosave
      re-POST; confirm `/heartbeat` fires on the interval and `remaining_s` tracks the timer; confirm the pre-check
      shows the registered photo (or capture-only on 404) and `/precheck` includes `identity_snapshot`.

- [ ] 11. Wire MCQ autosave on the client (R1). In the `.opt` click handler (~L1345) and `collectAnswers()` (~L1355)
      path, call `/autosave` debounced ~1.5s on each selection, and opportunistically on `visibilitychange` and
      `beforeunload`. Mirror selections to `localStorage` keyed by `attempt_id` as a belt-and-suspenders buffer that
      repopulates radio selections on reload when the server draft is unavailable.
      Files: `templates/assessment_take.html`
      Verify: in-browser, select answers and confirm debounced `/autosave` POSTs in the Network tab and a
      `localStorage` entry keyed by the attempt id; reload mid-exam and confirm selections repopulate; confirm no
      premature scoring/finalization occurs (status stays In Progress until explicit submit).

## Section 4 — Admin dashboard review + CSV export (`hr_assessments.html` + `Main.py`)

- [ ] 12. Add the HR event-review route `POST /api/hr/attempt/<int:attempt_id>/event/<int:event_id>/review`
      (`@login_required` + `@role_required(HR_ROLES)`), where `event_id` is `proctoring_events.id` (the PK already in
      the `SELECT *` rows), setting `review_status` (pending|confirmed|dismissed/false-positive) and `admin_notes`
      for that event. Candidate ownership never applies to HR routes; a candidate can never reach another
      candidate's data. The existing `/api/hr/attempt/<id>` (~L1396) already does `SELECT *` so the new columns
      surface with no query change.
      Files: `Main.py`
      Verify: `python -c "import Main"` succeeds; manual: as an HR user POST a review and confirm `review_status`/
      `admin_notes` persist on the row; as a non-HR user confirm redirect/deny.

- [ ] 13. Add the admin filter + CSV export route `GET /api/hr/attempts/export`
      (`@login_required` + `@role_required(HR_ROLES)`) that streams a CSV of proctoring events joined to attempts,
      filterable by candidate, exam, date range, severity, and `review_status`. Build the CSV with the stdlib `csv`
      module into an in-memory buffer and return it via `send_file`/`Response` with
      `Content-Type: text/csv` and a `Content-Disposition: attachment` filename. Parameterize all filter values in
      the SQL (no string interpolation).
      Files: `Main.py`
      Verify: `python -c "import Main"` succeeds; manual: as HR hit the route with and without filters and confirm a
      well-formed CSV downloads with the expected rows; confirm a non-HR user is denied.

- [ ] 14. Extend `templates/hr_assessments.html` review UI. In `openAttempt`/the event map (~L483–L517) surface the
      new columns (`incident_id`, `confidence`, `detection_source`, `review_status`) per event, add controls to mark
      each incident reviewed/confirmed/false-positive with a notes field (POST to the route from item 12), show the
      stored `identity_snapshot` side-by-side with the registered passport photo for human match, and add the
      filter inputs + a "Export CSV" button that calls the route from item 13. Keep this admin-only surface; no
      candidate-facing changes.
      Files: `templates/hr_assessments.html`
      Verify: as HR, open an attempt and confirm the new per-event fields render, submitting a review updates the row
      (re-open to confirm persistence), the identity snapshot vs passport photo panel shows, filters narrow the
      list, and Export CSV downloads a file.

---

## Cross-cutting constraints (apply to every item)

- Honesty (never violate): no code claims conclusive earbud/concealed-phone/devtools detection; earphones/headphones
  stay human-review indicators (`max_violations=-1`), never auto-terminate; no speaker→mic tone gate; uncertain AI
  predictions stay separate from confirmed misconduct (`detection_source` + `review_status`).
- Security: every candidate attempt route (new and rewritten) enforces `_own_attempt`; every admin route uses
  `@role_required(HR_ROLES)`. A candidate can never see another candidate's data or evidence.
- One clock: all elapsed/deadline/staleness math uses `TIMESTAMPDIFF(SECOND, started_at, NOW())` in the DB; all new
  raw writes use `NOW()`. Never introduce `UTC_TIMESTAMP()`.
- Migration is additive, idempotent, non-destructive, flavor-portable; applied before deploying the updated
  `Main.py` (R3). Mirror every new column in the ORM models.
- Preserve ALL unrelated features (SSO, IceWarp/IMAP login, profile, applications, requisitions, HR question CRUD,
  existing video review, whiteboard autosave). No placeholder core implementations; do not silently swallow
  exceptions.

## Gaps / assumptions

- No automated test suite exists in the repo; item 5 introduces a small `tests/` module for the pure escalation
  logic. If pytest is not installed in the venv, use `python -m unittest`. All other verification is import/compile +
  SQL idempotency + manual route/browser smoke tests, since camera/mic/model behavior is only testable manually
  (per the design's Testability section).
- MySQL flavor (Oracle MySQL 8 vs MariaDB) and session timezone remain unconfirmed from the workspace; both are
  handled defensively by the guarded migration and the `NOW()`-relative time math, so no decision is deferred to the
  implementer.

---

## Implementation verification evidence (first iteration — completed)

All 14 plan items implemented in place. Deploy note (R3): apply
`migrations/0001_proctoring.sql` to MySQL BEFORE deploying the updated `Main.py`
(`mysql -u root -p hr_portal < migrations/0001_proctoring.sql`); `db.create_all()` never
ALTERs existing tables. The app logs a loud non-fatal warning at boot if the sentinel column
`proctoring_events.incident_id` is missing.

Automated checks run in this workspace (results):

- `python -m py_compile Main.py` → **OK** (no syntax errors).
- `python -m py_compile tests/test_violation_engine.py` → **OK**.
- Jinja parse of `templates/assessment_take.html` and `templates/hr_assessments.html`
  (`jinja2.Environment().parse(...)`, jinja 3.1.6) → **OK** for both.
- Pure escalation-logic verification (extracted `decide_violation_action` + `PROCTOR_CFG`
  + `HARD_SERVER_TYPES` directly from `Main.py` source via AST, then exercised the same
  assertions as `tests/test_violation_engine.py`) → **17/17 PASS**, covering: warn-only
  default never escalates; first-warning threshold; `-1` indicators (earphones/headphones/
  clipboard) never terminate; uncertain-AI types (device_detected, eye_away, no_face,
  multiple_faces, mouth_movement, camera_covered, feed_frozen) never terminate; count-
  exceeded only escalates under opt-in `auto_action`; hard server types (tab_switch,
  window_blur, fullscreen_exit, second_display) escalate under opt-in; `require_human`
  blocks non-hard types; at-ceiling warns, over-ceiling escalates.
- Verified the server `PROCTOR_CFG['max_violations']` whitelist keys cover every real
  client `trigger()` event string in `assessment_take.html` (tab_switch, window_blur,
  fullscreen_exit, second_display, devtools_open, meta_key, alt_tab, capture_shortcut,
  keyboard_shortcut, clipboard, no_face, multiple_faces, device_detected, eye_away,
  mouth_movement, noise, camera_off, camera_covered, feed_frozen, network_loss) plus the
  indicator-only additions.

Environment note: the active system interpreter (Python 3.12, Flask 3.1.3) is NOT the
project venv — it lacks `flask_sqlalchemy`, `flask_login`, `pymysql`, `msal` and carries a
different Flask major than `requirements.txt` (Flask 2.3.3). Importing `Main` directly (and
thus `python -m pytest tests/test_violation_engine.py`) must be run inside the project venv;
it was NOT run here to avoid downgrading/polluting the system interpreter. The committed
`tests/test_violation_engine.py` imports from `Main` and is the authoritative test to run in
that venv; the AST-extraction logic check above verifies the exact committed source in the
meantime. DB-dependent paths (migration idempotency, route 403s, dedup, deadline, autosave,
heartbeat, CSV) require a running MySQL `hr_portal` and are manual smoke tests per the
design's Testability section.

## Re-verification pass (confirming the in-place changes are present on disk)

Re-ran verification against the current working tree to confirm all 14 plan items are present
in the real files (not a worktree). Confirmed in place:

- `Main.py`: `PROCTOR_CFG` + `SNAPSHOT_MAX=400000` + `validate_proctor_cfg()` (called at import);
  `HARD_SERVER_TYPES` / `UNCERTAIN_AI_TYPES`; `decide_violation_action()`; `_own_attempt()` IDOR
  helper applied to every candidate attempt route (`/violation`, `/whiteboard`, `/submit`,
  `/precheck`, `/registered-photo`, `/heartbeat`, `/autosave`, `/record-chunk`, `/record-finish`);
  rewritten `/violation` engine (whitelist clamp, incident dedup within `warning_cooldown_ms`,
  per-type counter, `detection_source='client'`, finalize-from-draft on auto_submit/terminate);
  hardened `/submit` (duplicate guard, `TIMESTAMPDIFF(SECOND, started_at, NOW())` deadline, draft
  fallback, `NOW()` timestamps); new `/precheck` (json.dumps report, `identity_snapshot` capped at
  `SNAPSHOT_MAX`), `/heartbeat` (server-side `network_loss` on stale beat, `detection_source='server'`,
  never terminates), `/autosave` (status-gated, A/B/C/D validation, last-write-wins JSON),
  `/registered-photo`; HR `/api/hr/attempt/<id>/event/<event_id>/review` + `/api/hr/attempts/export`
  (stdlib csv, fully parameterized filters); ORM models mirror every new column.
- `migrations/0001_proctoring.sql`: guarded information_schema dynamic-SQL per column + index;
  additive/idempotent; includes `draft_answers` (R1).
- `templates/assessment_take.html`: `window.PROCTOR_CFG = {{ proctor_cfg|tojson }}`, `CFG` derived
  from it with `HANDHELD_SLACK`, dead `noiseDb:26` removed; `trigger()` sends
  `incident_id`/`duration_ms`/`confidence` (confidence only for `device_detected`); autosave
  (debounced + beacon + localStorage), heartbeat interval, offline/online -> `network_loss`,
  identity capture + `/registered-photo` fetch into pre-check `/precheck` body.
- `templates/hr_assessments.html`: per-event `incident_id`/`confidence`/`detection_source`/
  `review_status` + notes + review POST; identity-snapshot panel; filter inputs + Export CSV.

Automated checks run this pass (results):

- `python -m py_compile Main.py` -> **OK**.
- `python -m py_compile tests/test_violation_engine.py` -> **OK**.
- Jinja parse of both templates (jinja 3.1.6) -> **OK** for both.
- Escalation-logic + whitelist-coverage check (AST-extracted `decide_violation_action` +
  `PROCTOR_CFG` + `HARD_SERVER_TYPES` + `UNCERTAIN_AI_TYPES` from the live `Main.py`, exercised with
  the committed test assertions plus a client-event whitelist-coverage assertion) -> **20/20 PASS**.

Environment note (unchanged): the active interpreter is Python 3.12.3 with Flask + jinja2 + msal but
WITHOUT `flask_sqlalchemy` / `flask_login` / `pymysql`, so `import Main` (and thus
`python -m pytest tests/test_violation_engine.py`) must be run in the project venv; it was not run
here to avoid polluting the system interpreter. No project venv exists on disk at `venv/`. The
workspace is NOT a git repository, so no commit was made.

## Confirmation pass (current step — all 14 items verified present on disk)

Re-audited the real working-tree files (no worktree) against the locked design + R1–R4. All four
files carry the complete, consistent implementation:

- `Main.py`: `SNAPSHOT_MAX=400000`; `PROCTOR_CFG` + `validate_proctor_cfg()` (called at import);
  `HARD_SERVER_TYPES` / `UNCERTAIN_AI_TYPES`; pure `decide_violation_action()`; `_own_attempt()`
  IDOR helper on every candidate attempt route; rewritten `/violation` (whitelist clamp with
  unknown-but-stored/action=ignore — never dropped; confidence only for `device_detected`; incident
  dedup within `warning_cooldown_ms`; distinct-incident per-type counter; `detection_source='client'`;
  `_finalize_from_draft` on auto_submit/terminate; exceptions logged, never swallowed); hardened
  `/submit` (duplicate guard, `TIMESTAMPDIFF(SECOND, started_at, NOW())` deadline, draft fallback,
  `NOW()` timestamps); `/precheck` (`json.dumps`, 16KB guard, `identity_snapshot` capped at
  `SNAPSHOT_MAX`), `/registered-photo` (404 → capture-only), `/heartbeat` (server-side `network_loss`
  with `detection_source='server'`, never terminates, `remaining_s` on the same clock),
  `/autosave` (status-gated, A/B/C/D + qid validation, last-write-wins JSON); HR
  `/api/hr/attempt/<id>/event/<event_id>/review` (scoped by event_id AND attempt_id) and
  `/api/hr/attempts/export` (stdlib csv, fully parameterized filters); ORM models mirror every new
  column (`identity_verified` = `db.SmallInteger`, matching SQL `SMALLINT`).
- `migrations/0001_proctoring.sql`: guarded information_schema dynamic-SQL per column + index;
  additive/idempotent/flavor-portable; includes `draft_answers` (R1).
- `templates/assessment_take.html`: `window.PROCTOR_CFG = {{ proctor_cfg|tojson }}`, `CFG` derived
  from it with `HANDHELD_SLACK`, dead `noiseDb:26` removed, noise derived from the server block;
  `trigger()` sends `incident_id`/`duration_ms`/`confidence` (confidence only for `device_detected`,
  stable incident id per continuous incident); autosave (debounced + sendBeacon + localStorage),
  heartbeat interval + server-clock reconcile, offline/online → `network_loss`, identity capture
  (`captureIdentitySnapshot`, ≤320px) + `/registered-photo` into the `/precheck` body.
- `templates/hr_assessments.html`: per-event `incident_id`/`confidence`/`detection_source`/
  `review_status` + notes + review POST; identity-snapshot panel; filter inputs + Export CSV.

Automated checks run this step (results):

- `python -m py_compile Main.py` → **OK** (exit 0).
- `python -m py_compile tests/test_violation_engine.py` → **OK** (exit 0).
- Jinja parse of both templates (`jinja2.Environment().parse`) → **OK** for both.

No code changes were needed this step — the LOCKED design was already implemented in place and is
correct. The workspace is still NOT a git repository, so (per the no-commit-when-not-a-repo rule) no
commit was made; this is the most likely reason the change was not visible as a diff/history.

## Re-audit + re-verification pass (current step)

Audited the real on-disk files (no worktree) item-by-item against the locked design + R1–R4; all
14 plan items are present and consistent. Fresh automated checks run this step:

- `python -m py_compile Main.py` → **OK** (exit 0).
- `python -m py_compile tests/test_violation_engine.py` → **OK** (exit 0).
- Escalation-logic + whitelist-coverage check (AST-extracted `decide_violation_action`,
  `PROCTOR_CFG`, `HARD_SERVER_TYPES`, `UNCERTAIN_AI_TYPES` from the live `Main.py`, exercised with
  the committed test assertions + a client-event whitelist-coverage assertion) → **20/20 PASS**.
  Confirmed every real client `trigger()` event string in `assessment_take.html`
  (alt_tab, camera_covered, camera_off, capture_shortcut, clipboard, device_detected, devtools_open,
  eye_away, feed_frozen, fullscreen_exit, keyboard_shortcut, meta_key, mouth_movement,
  multiple_faces, network_loss, no_face, noise, second_display, tab_switch, window_blur) is covered
  by `PROCTOR_CFG['max_violations']`.
- Jinja parse of both templates (jinja 3.1.6) → **OK** for both.

Environment note (unchanged): the active interpreter lacks `flask_sqlalchemy`/`flask_login`/`pymysql`
and no venv exists on disk, so `import Main` / `python -m pytest tests/test_violation_engine.py` must
be run in the project venv; the AST logic check verifies the exact committed source meanwhile.
DB-dependent paths (migration idempotency, 403 IDOR checks, dedup, deadline, autosave, heartbeat, CSV)
remain manual smoke tests against a running MySQL `hr_portal`.

## Review-fix pass (.agents/review.json CHANGES_REQUESTED → 12 blocking findings)

The consolidated gate returned CHANGES_REQUESTED. All 11 code-fixable blocking findings were fixed
in place; finding #12 (DB-backed runtime verification) is documented as not executable in this
environment (no project venv + no MySQL).

- **#1 Stored XSS (identity snapshot):** `/precheck` now validates the snapshot is a decodable
  jpeg/png/webp data URL (`_parse_image_data_url`, base64 `validate=True`) and drops anything else
  before storing. The raw data URL is NO LONGER sent to the browser — `api_hr_attempt` strips
  `identity_snapshot` (and `draft_answers`/`precheck_report`) and exposes only a boolean
  `has_identity_snapshot`. New HR-only endpoint `GET /api/hr/attempt/<id>/identity-snapshot` decodes
  and serves the bytes with a FIXED image Content-Type + `X-Content-Type-Options: nosniff`. The HR
  page sets `img.src` as a DOM property (never string-interpolated markup). All event/admin text
  stays `escapeHtml`/`escapeAttr`-escaped.
- **#2 Side-by-side passport photo:** new HR-only `GET /api/hr/attempt/<id>/registered-photo` streams
  the attempt owner's `passport_photo`; the review modal renders Registered + Live capture together,
  each wired via `img.src` DOM property with onload/onerror fallbacks.
- **#3 CSV formula injection:** `_csv_safe()` prefixes any cell beginning with `= + - @` or a leading
  tab/CR with a single quote (OWASP). Query stays fully parameterized.
- **#4/#6 Finalization answer loss + non-transactional finalize:** single idempotent finalizer
  `_finalize_attempt()` shared by `/submit` and the violation terminal branch. It DELETEs prior
  answer rows then re-scores (idempotent, never doubles). The violation branch and `/submit` wrap the
  answer writes + terminal status in `conn.begin()/commit()`, returning the terminal action/success
  ONLY after commit; on failure they rollback (violation downgrades to `warn`, submit returns 500).
  `/submit` no longer hard-rejects an already-finalized attempt that carries fresher answers — it
  upgrades the result (never downgrades Terminated→Completed; a no-answer resubmit returns
  `duplicate:true`).
- **#5/#7 Client auto_submit:** `trigger()` now treats BOTH `terminate` and `auto_submit` as terminal
  — sets `terminated=true`, stops recording via `submitTest()` (which posts the freshest
  `collectAnswers()`), and navigates after server confirmation.
- **#6 Incident continuity:** client now tracks incident LIFETIME (`incidentLastSeen` +
  `INCIDENT_RECOVERY_MS`) independently of the send cooldown. One continuous condition keeps ONE
  `incident_id` and grows `duration_ms` across fires; the id rotates only after the detector recovers
  (quiet ≥ recovery window), not every cooldown.
- **#10 Malformed events:** `assessment_violation` coerces every client field to a string via `_s()`
  and bounds `event_type` to 50 chars BEFORE the whitelist lookup, so a non-string/oversized field
  can neither raise nor overflow the column; unknown types still take the logged `ignore` path.
- **#11 Whitelist:** removed `earphones_detected` from `max_violations` and `UNCERTAIN_AI_TYPES`;
  verified EXACT contract equality (every emitted client `trigger()` type is a whitelist key; the
  only extra keys are the approved indicator/server labels `headphone_indicator`,
  `earphone_indicator`, `device_disconnect`, `network_loss`).
- **#3 (client) Precheck fail-open + passed-always-false:** the Start handler derives `passed` from
  the critical-check state BEFORE disabling the button, and the exam starts ONLY after `/precheck`
  returns `{ok:true}`; on any non-2xx/error it restores the Start/retry state and blocks start.
- **#12 WSGI migration check:** `_check_proctoring_migration()` now runs via one-time
  `run_startup_checks()` (idempotent `_startup_done` guard) invoked at module import AND from
  `__main__`, so an imported WSGI deployment also gets the R3 warning.

Automated checks run this step (results):

- `python -m py_compile Main.py` → **OK** (exit 0).
- `python -m py_compile tests/test_violation_engine.py` → **OK** (exit 0).
- Jinja parse of `assessment_take.html` + `hr_assessments.html` (jinja 3.1.6) → **OK** for both.
- `node --check` (v26) on the extracted/Jinja-stripped `<script>` of both templates → **OK** for both.
- Escalation + honesty + whitelist-contract check (AST-extracted `decide_violation_action` /
  `PROCTOR_CFG` / `HARD_SERVER_TYPES` / `UNCERTAIN_AI_TYPES` from the live `Main.py`) → **21/21 PASS**,
  including: warn-only default never escalates; `-1` indicators and all uncertain-AI types never
  terminate even under `terminate`+no-human; hard server types escalate only under opt-in; exact
  whitelist equality (`earphones_detected` gone, every emitted client type covered, no unapproved
  extras).

**Finding #12 (DB-backed runtime verification) — NOT EXECUTED here, requires project venv + MySQL.**
Migration idempotency re-run, 403 ownership, incident dedup, deadline, autosave, heartbeat, and CSV
export are DB-dependent and could not run because the active interpreter is not the project venv
(no `flask_sqlalchemy`/`flask_login`/`pymysql`, no venv on disk) and MySQL `hr_portal` is unavailable
in this environment. These must be run in the deployment-compatible environment before final sign-off.
