# Design Review — Live-Exam Anti-Cheating / Proctoring Module

Reviewer: design-review subagent (fresh, no authoring context)
Design under review: `.agents/proctoring-design.md`
Sources verified: `Main.py`, `templates/assessment_take.html`, `templates/hr_assessments.html` (referenced), `templates/pre_check.html` (referenced), `requirements.txt`

This design has already been through two prior review cycles; most of the structural problems (event-type naming, wrong client function, non-portable `ADD COLUMN IF NOT EXISTS`, forked noise config, timezone handling) are resolved and I re-verified each against the live source. The findings below are what remains after that verification. Verdict is mechanical: count of HIGH + MEDIUM findings.

---

## Findings

### 1. [MEDIUM] Reconnect "re-save answers" has no server endpoint to save MCQ answers mid-exam

**Where:** Decision 8 ("on reconnect **re-saves answers** (autosave already exists for whiteboards; extend to MCQ selections)") and the Files-to-change note for `assessment_take.html`.

**Problem:** Verified in `assessment_take.html`: whiteboard autosave posts to `/api/assessment/<id>/whiteboard` (line ~1327, `wbDirty`), but **MCQ selections are only persisted at `/submit`** (`Main.py` line ~1216, the `INSERT INTO assessment_answers` loop runs inside the submit handler, which also finalizes/scores and flips `status` to `Completed`/`Terminated`). There is no incremental MCQ-save route, and no `localStorage` buffering exists (grep confirmed). "Extend autosave to MCQ selections" therefore implies a new flow that the design names but does not specify: what endpoint receives the periodic MCQ state, what table/column it writes, and how it avoids the `/submit` duplicate-submit guard (which rejects anything not `In Progress`). Re-using `/submit` for autosave would prematurely finalize and score the attempt.

**Fix (pick one, specify it):**
- (a) Add a dedicated `POST /api/assessment/<attempt_id>/autosave` (`@login_required` + ownership + `status=='In Progress'` guard) that upserts partial MCQ selections into a scratch store (e.g. `assessment_attempts.draft_answers JSON`, added via the same guarded migration pattern) **without scoring**; `/submit` reads the draft as a fallback. Document the body `{answers: {qid: selected}}` and the clamp/validation rules, and have the client re-POST it on `online`.
- (b) Keep answers client-side in `localStorage` keyed by `attempt_id` and re-apply on reload, and state explicitly that **no server autosave is added** (so the "extend to MCQ" claim is dropped). This is lighter but loses answers if the device dies.

Either is acceptable, but the design must name one and define the endpoint/column or the storage key, because "re-save answers" is currently an unimplementable instruction for the coder.

---

### 2. [NIT] New candidate routes specify only `@login_required`, but every existing candidate route also uses `@login_required_with_session`

**Where:** Decision 7 (`/precheck`, `/registered-photo`, `/heartbeat` are "`@login_required`"); the existing `/violation` and `/submit` are also only `@login_required`.

**Problem:** Verified in `Main.py`: every candidate-facing page/route (`candidate_dashboard`, `take_assessment`, `start_assessment`, etc.) stacks `@login_required` **and** `@login_required_with_session` (lines 643, 1064, 1009…). The attempt-scoped API routes (`/violation`, `/submit`) currently use only `@login_required`. The design adds an ownership helper (good, closes the IDOR) but does not say the new routes should also carry `@login_required_with_session` to match the session-binding behavior the rest of the candidate surface relies on.

**Fix:** State that the new candidate routes use `@login_required` + `@login_required_with_session` + `_own_attempt(attempt_id)`, matching the existing candidate-route decorator stack. (Ownership via `candidate_email == current_user.email` already covers the core IDOR; this is about consistency with the session guard.)

---

### 3. [NIT] Migration adds columns but nothing triggers it; `db.create_all()` will not ALTER existing tables

**Where:** Decision 2 ("applied manually: `mysql -u root -p hr_portal < migrations/0001_proctoring.sql`") and the model edits.

**Problem:** This is correctly flagged as a manual run, which is fine. The latent risk worth stating explicitly: SQLAlchemy `db.create_all()` (if called at startup anywhere) creates missing *tables* but never ALTERs existing ones, so the new model columns would silently not exist until the `.sql` is run by hand. If the raw-PyMySQL routes run before the migration, inserts referencing `identity_snapshot`/`precheck_report`/`duration_ms`/etc. fail at runtime.

**Fix:** Add a one-line deploy note: the `.sql` migration **must** be run before deploying the new `Main.py`, and the app does not auto-migrate. Optionally gate the new columns' writes behind a startup `information_schema` presence check that logs a clear error if the migration has not been applied. No code change to the migration itself is required.

---

### 4. [NIT] `detection_source` default `'client'` on the column vs server-minted events

**Where:** Decision 2 migration (`detection_source VARCHAR(16) NOT NULL DEFAULT 'client'`) and Decision 9 (server writes `detection_source='server'` for prolonged-loss).

**Problem:** The column default is `'client'`. The server-side prolonged-loss / `network_loss` / `device_disconnect` writes must therefore pass `detection_source='server'` explicitly on insert — if any server-side insert omits the field it will be mislabeled as client-originated, defeating the client-vs-server separation that is a hard requirement. The design states the intent but relies on every raw insert remembering to set it.

**Fix:** Note in Decision 9 that **all** server-originated inserts set `detection_source` explicitly (never rely on the column default for server events); the default `'client'` exists only so the existing `/violation` path stays backward-compatible. A quick code-review checklist item, not a design change.

---

## Verified Assumptions (checked against source — correct)

1. **Client `/precheck` call exists, server route does not.** `assessment_take.html` line ~1902 POSTs to `API + '/precheck'`; grep of `Main.py` routes shows no `/precheck` handler. Design's "the route is missing" is accurate.
2. **`started_at` is written with MySQL `NOW()`**, not `UTC_TIMESTAMP()` — `Main.py` line ~1053 `VALUES (…,'In Progress',NOW())`. The design's one-clock `TIMESTAMPDIFF(SECOND, started_at, NOW())` deadline fix is correct and the dropped `UTC_TIMESTAMP()` plan was right to drop.
3. **Noise literals match.** `assessment_take.html` line ~1064 `CFG.noiseDb = Math.max(18, noiseBaseline + 14)`, line ~327 `noiseMs: 1200`, and a dead static `noiseDb: 26` (line ~328) that is overwritten at calibration. The design's `floor_db:18, margin_db:14, sustain_ms:1200` and the "remove the dead 26" note are exact.
4. **`trigger(type, severity, desc)` is the real client funnel** (line ~1462) with `armedAt` grace gate, per-type `cd[type]` vs `CFG.cooldownMs` (line ~1466), internal snapshot capture, and `/violation` POST. No `sendViolation` exists. Design targets the correct function.
5. **Client-emitted `event_type` strings exactly match `PROCTOR_CFG['max_violations']` keys:** `keyboard_shortcut, tab_switch, window_blur, fullscreen_exit, second_display, devtools_open, meta_key, alt_tab, capture_shortcut, clipboard, camera_off, camera_covered, feed_frozen, noise, multiple_faces, no_face, device_detected, eye_away, mouth_movement` (grep of `trigger('…')` calls). The four new ones (`headphone_indicator, earphone_indicator, device_disconnect, network_loss`) are honestly flagged as not emitted today. All fit `event_type VARCHAR(50)`.
6. **`confidence` only meaningful for `device_detected`.** `trigger('device_detected', …, Math.round(best.score*100)…)` carries a real COCO score; `multiple_faces` carries a person **count** (`dets.length` / `cocoPeople`), line ~1720/1755. Design's "confidence null except device_detected" is correct.
7. **Schema columns verified present:** `assessment_attempts` has `candidate_email` (ownership), `warning_count`, `first_warning_reason`, `termination_reason`, `terminated_at`, `video_path`, `status`, `started_at` (lines 419–435). `proctoring_events` has exactly `id, attempt_id, event_type(VARCHAR 50), severity, description(255), snapshot_b64(TEXT), occurred_at` (lines 451–460) — the migration's new columns do not collide with any existing column. `assessments.duration_minutes` exists (line 391) so the deadline JOIN is valid.
8. **`snapshot_b64` cap is 400000** in the live `/violation` route (`Main.py` line 1130). Design's shared `SNAPSHOT_MAX = 400000` matches.
9. **`/api/hr/attempt/<id>` uses `SELECT * FROM proctoring_events`** (line 1436), so new columns surface in the HR dashboard without a query change — design's claim holds.
10. **Infra matches:** `HR_ROLES = ['admin','hr','hr lead']`, `role_required`, `get_db_connection` (DictCursor) all exist (lines 56, 81, 105). No Alembic/Flask-Migrate. `requirements.txt` has no `face_recognition`/`dlib` — the "no new Python deps" and "reject dlib" claims are honest.
11. **CDN vision libs match exactly:** face-api.js 1.7.12, tfjs 4.10.0, coco-ssd 2.2.2, tasks-vision 1.1.0 (lines 280–282, 742). "No new vision library" is accurate; COCO-SSD remains the only object model.
12. **Honesty requirements met:** earbuds/headphones/hidden-phone are `max_violations: -1` (indicator-only, never auto-terminate); `auto_action` defaults to `warn_only`; `require_human_for_terminate: True`; no speaker→mic tone gate is reintroduced. All consistent with the module's hard limits.
13. **Whiteboard autosave exists; MCQ autosave does not** — basis for Finding 1.

## Unverified / Wrong Assumptions

- **None wrong.** Every concrete source claim the design makes (line numbers 1053/1055 for `started_at`, 1064 for noise, 327–328 CFG, the `trigger` signature, the event-type set, the `/precheck` gap, `SELECT *` HR endpoint) was checked and is accurate. The minor line-number drift (design cites `started_at` at line 1055; it is ~1053) is immaterial.
- **Not independently verifiable from the workspace (correctly disclaimed by the design):** the deployed MySQL flavor (Oracle MySQL 8.0 vs MariaDB) and the DB session timezone. The design does not assume either — it writes a flavor-portable `information_schema`-guarded migration and makes all time math `NOW()`-relative so it is tz-independent. These remain assumptions but are handled defensively, so they are not findings.

---

## Verdict

HIGH findings: 0
MEDIUM findings: 1 (Finding 1 — reconnect MCQ re-save has no defined endpoint/flow)
NIT findings: 3

MEDIUM count > 0 → **CHANGES_REQUESTED**.

One loop-back to pin down the MCQ-autosave endpoint (or explicitly choose client-side `localStorage` and drop the "extend to MCQ" server claim) and the design is ready. The three NITs are cheap to fold in at the same time.
