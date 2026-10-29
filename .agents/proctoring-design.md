# Design: Live-Exam Anti-Cheating / Proctoring Module (integrated)

## Overview

This integrates a production-ready proctoring layer **into the existing Candidate_portal Flask app**, reusing
every current mechanism rather than adding a parallel system. The exam page `templates/assessment_take.html`
already loads **face-api.js (TinyFaceDetector + 68-pt)**, **TF.js + COCO-SSD**, and **MediaPipe FaceLandmarker**,
runs a gating pre-check overlay, records webm chunks, and posts violations. The server (`Main.py`) already has
`ProctoringEvent`, `AssessmentAttempt` (with `warning_count`, `first_warning_reason`, `termination_reason`,
`terminated_at`, `video_path`), and routes for `/violation`, `/whiteboard`, `/submit`, `/record-chunk`,
`/record-finish`, `/admin/assessment/<id>/video`, `/api/hr/attempt/<id>`, `/hr/assessments`.

The work is therefore **evolution, not greenfield**: make the blunt violation route a configurable per-type
cooldown+dedup escalation engine, add the missing `/precheck` route, add server-side identity verification +
submit/deadline/heartbeat guards, enrich `proctoring_events` for human review, and consolidate the client CFG.
No current functionality (SSO, IceWarp/IMAP login, profile, applications, requisitions, HR question CRUD, video
review) changes behaviour.

**Technology stack (locked):** Python 3 + Flask 2.3.3, Flask-SQLAlchemy 3.1.1, Flask-Login 0.6.2, raw PyMySQL
1.1.0 (`get_db_connection()` DictCursor, `charset='utf8mb4'`, `autocommit=True`), MySQL 8 (`hr_portal`; the exact
flavor — Oracle MySQL vs MariaDB — is **not** confirmed from the workspace, so the migration is written to run on
either, see Decision 2), **no Alembic/Flask-Migrate** (migrations are hand-run `.sql`). Client: existing CDN face-api.js 1.7.12, TF.js 4.10.0 + COCO-SSD 2.2.2,
MediaPipe tasks-vision 1.1.0, Bootstrap 5. **No new Python packages and no new vision libraries are added.**

---

## Decision 1 — Identity verification: client-side capture + human match (chosen)

**Chosen: approach (a).** Reuse the MediaPipe FaceLandmarker + face-api detector already loaded on the exam page.
Rejected approach (b) `face_recognition`/`dlib`: it needs a native C++/CMake toolchain that is painful and
unreliable on Windows, adds a heavy dependency, and the goal explicitly forbids silently adding a face lib.
No automated 1:1 face-match claim is made — matching is a **human-review** step, which also avoids
mis-identifying unrelated people.

Mechanism, during the pre-check (before `startExam()`):

1. The registered photo already lives in `candidate_users.passport_photo` (`LargeBinary`) +
   `passport_photo_filename`. Add a tiny authenticated route `GET /api/assessment/<attempt_id>/registered-photo`
   (candidate-owned, see Decision 7) that streams the current user's `passport_photo` so the overlay can show it
   side-by-side with the live camera. If the candidate has **no** passport photo, identity verification degrades
   to "capture-only" (store live snapshot, `identity_verified=0`, flagged for manual review) — never a hard block,
   since many existing accounts predate photo upload.
2. The overlay requires one centred, single-face, well-lit frame (reusing the existing single-face / face-visible
   checks). On success the client captures a JPEG snapshot (downscaled to <=320px wide, quality ~0.7) and POSTs it
   with the pre-check payload to the new `/precheck` route (Decision below).
3. Server stores the snapshot in `assessment_attempts.identity_snapshot` and sets `identity_verified` per policy:
   `0` by default (pending human match) with `PROCTOR_CFG['identity']['require_match']=False` meaning the exam may
   proceed; the HR reviewer confirms/denies identity against `passport_photo` in the existing attempt-review UI.

**Verification-failure + retry UX:** if no single centred face is detected within the pre-check attempt, the
identity row shows "We couldn't confirm a clear single face" with a **Retry** button (reuses the existing pre-check
`Re-run checks` loop). Three consecutive failures surface guidance (improve lighting / face the camera / remove
obstruction) and a Cancel that returns to `/candidate-assessments`. No racial/identity claim text is ever shown
to the candidate — only "clear face captured / not captured".

---

## Decision 2 — Idempotent migration + model edits

**File: `migrations/0001_proctoring.sql`** (new). `ADD COLUMN IF NOT EXISTS` is a **MariaDB-only** extension and
raises `ER_PARSE_ERROR` on stock Oracle MySQL 8.0; the deployed flavor is not confirmed from the workspace, so the
script must not depend on it. Every column add therefore uses the **same guarded `information_schema` dynamic-SQL
pattern** already required for indexes (MySQL also lacks `ADD INDEX IF NOT EXISTS`). The script only ever `ADD`s —
never drops or recreates — and is safe to re-run on production regardless of flavor. One guarded block per column
is spelled out below (the pattern is mechanical and identical, so it can be generated rather than hand-typed).

```sql
-- 0001_proctoring.sql  — idempotent, re-runnable, additive only. DB: hr_portal
-- Portable column-add (works on both Oracle MySQL 8.0 and MariaDB): guard on information_schema.columns.

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

-- proctoring_events.confidence (NULL by default — most events carry no score; see Decision 6)
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
-- occurred_at already exists (DATETIME). New raw writes use NOW() to match started_at's clock (see Decision 9) — no change.

-- assessment_attempts.identity_verified  (SMALLINT to match ORM db.SmallInteger — see Decision 2 model note)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='identity_verified');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN identity_verified SMALLINT NOT NULL DEFAULT 0','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- assessment_attempts.identity_snapshot  base64 data-URL JPEG (text; see justification)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='identity_snapshot');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN identity_snapshot LONGTEXT NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- assessment_attempts.precheck_report  JSON (raw path must json.dumps — see Decision 2/5 note)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='precheck_report');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN precheck_report JSON NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- assessment_attempts.last_heartbeat
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='assessment_attempts' AND column_name='last_heartbeat');
SET @s := IF(@c=0,'ALTER TABLE assessment_attempts ADD COLUMN last_heartbeat DATETIME NULL','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- guarded index creation (same pattern on information_schema.statistics)
SET @i := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND index_name='ix_pe_incident');
SET @s := IF(@i=0,'CREATE INDEX ix_pe_incident ON proctoring_events (incident_id)','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @j := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='proctoring_events' AND index_name='ix_pe_attempt_incident');
SET @s := IF(@j=0,'CREATE INDEX ix_pe_attempt_incident ON proctoring_events (attempt_id, incident_id)','SELECT 1');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;
```

No FK is added for `incident_id` (it is a server-minted logical id, not a row ref). `attempt_id` already carries an
index via the model. The migration is applied manually: `mysql -u root -p hr_portal < migrations/0001_proctoring.sql`.

**`identity_snapshot` type = `LONGTEXT`** (not `LargeBinary`): the client already produces a base64 data-URL JPEG,
and `proctoring_events.snapshot_b64` is already `TEXT`; keeping snapshots as base64 text matches the existing
evidence pattern (no BLOB/multipart handling to add) and the HR review endpoint returns `SELECT *` as JSON, so a
text column renders directly. `precheck_report` = native `JSON` so the dashboard can read structured per-check results.

**Type-agreement note (Finding 5):** `identity_verified` is **`SMALLINT`** in both the SQL above and the ORM
(`db.SmallInteger`) so the model describes the real column — no SMALLINT/TINYINT drift. Because `/precheck` and
`/heartbeat` are **raw-PyMySQL** routes (not ORM), they must `json.dumps(precheck_report)` before insert (a Python
dict passed raw would be stored as its `repr`), and store `identity_snapshot` as the plain text data-URL string.

**Matching SQLAlchemy edits in `Main.py`** (so ORM reads/writes stay consistent with the DB; these are plain
column adds — no table recreate):

```python
class ProctoringEvent(db.Model):
    # ...existing...
    incident_id      = db.Column(db.String(64), index=True)
    duration_ms      = db.Column(db.Integer)
    confidence       = db.Column(db.Float)
    detection_source = db.Column(db.String(16), default='client')
    review_status    = db.Column(db.String(16), default='pending')
    admin_notes      = db.Column(db.Text)

class AssessmentAttempt(db.Model):
    # ...existing...
    identity_verified = db.Column(db.SmallInteger, default=0)
    identity_snapshot = db.Column(db.Text)           # LONGTEXT
    precheck_report   = db.Column(db.JSON)
    last_heartbeat    = db.Column(db.DateTime)
```

The raw-PyMySQL routes don't need the ORM, but keeping the models in sync prevents drift and lets other code use the ORM.

---

## Decision 3 — Server `PROCTOR_CFG` + mirrored client `CFG`

A single source-of-truth dict `PROCTOR_CFG` is added near the top of `Main.py` (after `HR_ROLES`). Defaults are
**conservative**. A `validate_proctor_cfg()` runs once at import: asserts numeric ranges (durations 200–60000ms,
confidences 0–1, counts >=1), clamps out-of-range values, logs a warning, and never raises (bad config must not
crash exam serving). The exam page already injects values via Jinja; the client `CFG` block (currently device-aware
constants) is **consolidated** to read server defaults rendered into the template, keeping device slack as a
multiplier — we do **not** fork a second config.

```python
PROCTOR_CFG = {
  "grace_period_ms":        7000,     # armDelayMs — no violations fire before this after Start
  "detect_every_ms":        550,      # camera processing interval (handheld multiplied x1.6 client-side)
  "face_visibility_ms":     1500,     # noFaceMs — sustained no/obscured face (emits no_face)
  "multiple_face_ms":       1200,     # sustained 2nd face before violation (emits multiple_faces)
  # gaze + look-down + head-turn ALL surface through the single eye_away event today:
  "gaze_sustain_ms":        1300, "gaze_glance_count": 3, "gaze_glance_window_ms": 20000,
  "head_pose_sustain_ms":   1400, "yaw_degrees": 16, "pitch_degrees": 13,
  # COCO device sweep -> device_detected (the ONLY event type that carries a real 0-1 confidence; multiple_faces
  # is a person count and stores confidence=null — Finding 4):
  "phone_confidence":       0.30, "phone_sustain_ms": 1000,   # cell phone/book/remote floor
  "device_confidence":      0.45,                             # laptop/tv floor (avoid exam screen)
  "headphone_indicator":    True,     # treat as SUSPICIOUS human-review indicator only (Decision 5)
  "noise": {"calibration_ms": 3000, "margin_db": 14, "floor_db": 18, "sustain_ms": 1200,
            "grace_period_ms": 7000},  # threshold = max(floor_db, baseline + margin_db); never a fixed universal value.
                                       # Values chosen to MATCH the live client calibration (floor 18 / margin 14,
                                       # verified in assessment_take.html line 1064). The client derives from these
                                       # literals — see Decision 4 — so the two cannot drift.
  "warning_cooldown_ms":    4500,     # per-incident dedup window (server-enforced)
  # Keys are the EXACT event_type strings the client's trigger() calls emit today
  # (verified in assessment_take.html). -1 = indicator only, never auto-terminates.
  "max_violations": {
      "tab_switch": 3, "window_blur": 3, "fullscreen_exit": 3,
      "second_display": 2, "devtools_open": 3, "meta_key": -1, "alt_tab": 3,
      "capture_shortcut": 3, "keyboard_shortcut": 4, "clipboard": -1,
      "no_face": 4, "multiple_faces": 2, "device_detected": 2,
      "eye_away": 5, "mouth_movement": -1, "noise": 5,
      "camera_off": -1, "camera_covered": 4, "feed_frozen": 3,
      # NEW emitters added in this change (see Decisions 5 & 8), indicator-only:
      "headphone_indicator": -1, "earphone_indicator": -1,
      "device_disconnect": -1, "network_loss": -1},
  "escalation": {"first_warning_at": 1, "terminate_policy": "configurable",  # off|warn_only|auto_submit|terminate
                 "auto_action": "warn_only", "require_human_for_terminate": True},
  "evidence": {"capture_snapshots": True, "max_snapshot_px": 320, "jpeg_quality": 0.7,
               "store_raw_audio": False, "retention_days": 90},
  "network": {"heartbeat_ms": 15000, "server_timeout_s": 90},   # prolonged-loss threshold
  "identity": {"require_photo": False, "require_match": False},  # conservative: capture + human match
}
```

Default `auto_action="warn_only"` means **no AI detection auto-terminates** out of the box; an admin must opt into
`auto_submit`/`terminate`, and even then `require_human_for_terminate` keeps uncertain single detections from
ending an exam. The template renders a `window.PROCTOR_CFG = {{ proctor_cfg|tojson }}` block; the existing client
`CFG` constants are rebuilt from it (`eyeAwayMs = PROCTOR_CFG.gaze_sustain_ms`, etc.) with the handheld multiplier
retained. `take_assessment` passes `proctor_cfg=PROCTOR_CFG` to `render_template`.

**Noise is a derived value, not a second literal (Finding 2).** `PROCTOR_CFG['noise']` is the single source; the
client computes its runtime threshold from it rather than carrying its own numbers. The live client today hard-codes
`CFG.noiseDb = Math.max(18, noiseBaseline + 14)` / `noiseMs: 1200` and a stale static `noiseDb: 26` literal that is
overwritten at calibration time. This change replaces both with a derivation from the server block:

```js
// in assessment_take.html, where noiseBaseline is computed from the pre-check samples
CFG.noiseDb = Math.max(PROCTOR_CFG.noise.floor_db, noiseBaseline + PROCTOR_CFG.noise.margin_db); // 18 / 14 from server
CFG.noiseMs = PROCTOR_CFG.noise.sustain_ms;                                                      // 1200 from server
```

The static `noiseDb: 26` constant in the `CFG` literal is removed (it was dead — recalculated at calibration). With
server `floor_db:18, margin_db:14, sustain_ms:1200`, the derived client values are byte-for-byte the current live
numbers, so behaviour is unchanged while the config stops being forked.

---

## Decision 4 — Client JS: consolidate & reuse, add nothing

**Reused as-is:** face-api.js (TinyFaceDetector + 68-pt live loop), COCO-SSD object sweep (phone/book/laptop),
MediaPipe FaceLandmarker (gaze/pose/mouth), the pre-check overlay, chunked `MediaRecorder` upload,
`visibilitychange`/`blur`/fullscreen/`devtools`/`copy`/`cut`/`paste` listeners that already exist, and above all the
existing **`trigger(type, severity, desc)`** function — the single funnel that applies the grace gate
(`Date.now() < armedAt`), the **per-type client cooldown** (`cd[type]` vs `CFG.cooldownMs`), captures a JPEG
snapshot internally, POSTs `{ event_type, severity, description, snapshot_b64 }` to `/violation`, and acts on
`res.action` (`terminate`/`warn`). **No new vision library is added** — COCO-SSD remains the only object model.

Changes are consolidation only — there is **no** `sendViolation()`; all edits target `trigger()`:
- Replace the hard-coded `CFG` literal with values derived from `window.PROCTOR_CFG` (Decision 3), keeping the
  existing `IS_HANDHELD` slack as a multiplier so the device-aware behaviour is preserved.
- Extend `trigger(type, severity, desc)` to (a) **mint/track a stable `incident_id`** per continuous incident
  (reuse the existing `cd[type]` timing: a new id when a type fires after a cooldown gap, same id while it keeps
  re-firing within the window) and (b) add `duration_ms` and `confidence` to the existing POST body. The function
  keeps capturing the snapshot internally and keeps enforcing its per-type cooldown — the server dedups/clamps on
  top (Decision 9). `confidence` is set **only** for `device_detected` (COCO `best.score`, a true 0–1 probability);
  every other `trigger()` call passes `confidence=null` (Finding 4 & Decision 6). `multiple_faces` fires from two
  client paths (face-api `dets.length` and COCO `cocoPeople` count) — both produce a **person count, not a
  probability**, so it stays `confidence=null` and the count is carried in `description` ("N persons in frame").
- The `copy`/`cut`/`paste`/`contextmenu`/`keydown` restriction listeners already exist and already call
  `trigger('clipboard'/'keyboard_shortcut'/…)`; no new blocking logic is added. Add only `offline`/`online`
  handlers that fire a new `network_loss` client event, and `camera_off` already covers device loss.
- Add a `setInterval` heartbeat POST (Decision 8) and reconnect-triggered answer re-save.
- Add the identity-capture step into the existing pre-check flow and extend the `/precheck` POST body with the
  captured `identity_snapshot` data-URL (Decision 1).

---

## Decision 5 — Honesty / limits (hard requirement)

Webcams, mics, and the chosen models **cannot** reliably detect in-ear/concealed earbuds or hidden phones. The
design therefore:
- Treats **earphones/headphones as a SUSPICIOUS human-review indicator only** (`detection_source` recorded,
  `review_status='pending'`, `max_violations=-1` so it **never** auto-terminates). The mandatory recorded
  ear-exposure challenge (the existing left/right head-turn clip exposing each ear) plus the browser
  **audio-output-device label check** (`enumerateDevices()` output labels) and a short ear-view clip are stored as
  evidence for a human, not as proof.
- **Does not** reintroduce the removed speaker->mic tone test as a hard gate (it stays absent).
- Keeps **client-reported events separate from server-verified**: every client event is stored with
  `detection_source='client'`, and the server **validates/clamps** all client fields (type whitelist, severity
  enum, `confidence` clamped 0–1, `duration_ms` clamped 0–600000, snapshot length capped). Arbitrary client
  violation rows are never trusted to drive termination without server policy.
- **Privacy:** the pre-check overlay states plainly what is monitored (camera, mic level, screen, focus/tab). PII
  is minimised — snapshots downscaled to <=320px, **no raw audio stored** (`store_raw_audio=False`; only derived dB
  level), identity snapshot access restricted to the owner + HR. Evidence `retention_days=90` with a documented
  deletion job; admins can trigger deletion from the review UI.

---

## Decision 6 — Dependencies

**Python side: none added.** All server work uses Flask / Flask-SQLAlchemy / PyMySQL already in `requirements.txt`.
**Client side: none added** — all four vision/CDN libs are already wired. No `face_recognition`/`dlib` (explicitly
rejected in Decision 1). If a future admin demands automated 1:1 match, that is a separate, explicitly-surfaced
decision — not silently introduced here.

Install command (unchanged env, documented for completeness):
`.\venv\Scripts\pip install -r requirements.txt` — no new lines.

---

## Decision 7 — Routes, APIs, ownership, error handling

**New: `POST /api/assessment/<attempt_id>/precheck`** (`@login_required`). The client already calls this; the route
is missing. Validates ownership and stores the pre-check result. The **full accepted body matches what the client
sends today** (`device_type, device_info, noise_baseline, passed, report`) **plus** `identity_snapshot`, which is
added to the client payload in this change (Decisions 1 & 4). Validated fields:
- `device_type` — enum (`desktop`/`laptop`/`tablet`/`mobile`/`handheld`/other); unknown -> stored as `'unknown'`.
- `device_info` — str, truncated to `<=255`.
- `noise_baseline` — float, clamped `0–120` dB.
- `passed` — bool.
- `report` — dict, serialized with `json.dumps`, size-capped (reject/truncate if `>16 KB`).
- `identity_snapshot` — optional str, length-capped at the exact `SNAPSHOT_MAX = 400000` constant (Finding 3), the
  **same** cap the existing `/violation` route applies to `snapshot_b64`; both routes reference the one module-level
  constant so there is a single snapshot-size limit across the app. Stored verbatim as a data-URL.

Storage: `device_info` + `passed` are folded into the `precheck_report` JSON (so no extra columns are needed);
`noise_baseline` is kept inside that JSON too; `identity_snapshot` -> `assessment_attempts.identity_snapshot`;
`identity_verified` is set per policy (default `0`, pending human match). Returns `{"ok":true}` (or `{"ok":false}`
500 on DB error so the client can show "pre-check save failed, Retry").

**New: `GET /api/assessment/<attempt_id>/registered-photo`** — streams the owner's `passport_photo` for side-by-side.

**New: `POST /api/assessment/<attempt_id>/heartbeat`** — updates `last_heartbeat=NOW()`; returns server time plus
`remaining_s` computed with the **identical `TIMESTAMPDIFF(SECOND, started_at, NOW())` formula** used by `/submit`
(Finding 1) so the client reconciles the deadline against the same clock after reconnection. Using `NOW()` here too
keeps `last_heartbeat`, `started_at`, and the submit timestamps on one clock.

**Rewritten: `POST /api/assessment/<attempt_id>/violation`** — now the escalation engine (Decision 10). Still
`@login_required`; **adds ownership enforcement** (see below).

**Hardened: `POST /api/assessment/<attempt_id>/submit`** — add server-side **duplicate-submit guard** (reject if
`status != 'In Progress'` -> return existing result, do not re-score) and **deadline validation** that is
**timezone-agnostic (Finding 1)**. The source writes `started_at` with MySQL `NOW()` (session timezone), **not**
`UTC_TIMESTAMP()` — verified in `Main.py` line 1055 (`VALUES (…,'In Progress',NOW())`). Comparing `started_at +
duration` against `UTC_TIMESTAMP()` is therefore only correct when the DB session tz happens to be UTC, which is
unconfirmed, so the deadline could mis-fire by the UTC offset. Instead, compute elapsed time **entirely inside the
DB against the same clock that wrote `started_at`** (`NOW()`), making the comparison independent of session vs UTC:

```sql
SELECT (TIMESTAMPDIFF(SECOND, at.started_at, NOW()) > a.duration_minutes * 60)                       AS past_deadline,
       GREATEST(a.duration_minutes * 60 - TIMESTAMPDIFF(SECOND, at.started_at, NOW()), 0)            AS remaining_s
FROM assessment_attempts at JOIN assessments a ON a.id = at.assessment_id
WHERE at.id = %s
```

`duration_minutes` lives on `assessments` (joined, as in `api_assessment_take`). If `past_deadline` is true, accept
the answers but force `status` per policy and record `termination_reason='deadline exceeded'`. The existing
`submitted_at=NOW()` / `terminated_at=IF(%s, NOW(), …)` writes **stay on `NOW()`** so every attempt-timing column
uses one consistent clock (the earlier plan to migrate some writes to `UTC_TIMESTAMP()` is dropped — mixing clocks
is exactly what caused the offset risk). Preserve answers on any terminate/auto-submit.

**Ownership (security fix):** today `/violation` and `/submit` only check `@login_required` and trust the URL
`attempt_id`. Every attempt-scoped route must verify
`attempt.candidate_email == current_user.email` (candidate routes) or `@role_required(HR_ROLES)` (admin routes),
returning `403` otherwise. This closes a real IDOR hole and is enforced via a small helper `_own_attempt(attempt_id)`.

**Error handling per operation** (concrete):

| Operation | Failure | Recoverable? | Caller receives | Logged |
|---|---|---|---|---|
| `/precheck` DB write | `get_db_connection()` None / exec error | yes | `{"ok":false}` 500, client shows "pre-check save failed, Retry" | `print()` server log |
| ownership check | email mismatch / no row | fatal for request | `403 {"ok":false,"error":"forbidden"}` | log attempt_id + email |
| `/violation` not In Progress | attempt done/terminated | yes | `{"action":"ignore"}` | no |
| `/violation` dup within cooldown | same `incident_id` in window | yes | `{"action":"noop"}` | no |
| `/submit` already submitted | `status!='In Progress'` | yes | `{"ok":true,"status":<existing>,"duplicate":true}` | no |
| `/submit` deadline exceeded | `TIMESTAMPDIFF(SECOND,started_at,NOW()) > duration*60` | yes | `{"ok":true,"status":...,"late":true}` | yes |
| `/heartbeat` stale | now - last_heartbeat > timeout | n/a (server detects) | `{"ok":true,"server_time":...,"remaining_s":N}` | mark event if prolonged |
| `registered-photo` missing | no `passport_photo` | yes | `404`; client shows capture-only mode | no |
| client model load fail | CDN/weights error | partial | existing non-fatal path keeps COCO+audio+focus active | client console |
| camera/mic lost mid-exam | device/permission revoked | yes | existing `camera_off` client event (never auto-cheat) | `proctoring_events` |

**External-input validation summary:** `event_type` (required; **whitelist = the keys of
`PROCTOR_CFG['max_violations']`**, which are exactly the strings the client emits — see Decision 3 so the two
cannot drift), `severity` (optional, enum Low/Medium/High, default Medium), `description` (optional, <=255),
`snapshot_b64` (optional, `<= SNAPSHOT_MAX` = 400000, the shared constant also used by `/precheck` — Finding 3),
`confidence` (**optional, nullable**: accepted only for `device_detected`; clamped `0–1`; all other events —
including `multiple_faces`, which carries a person *count*, not a probability — send and store `null` — Finding 6), `duration_ms`
(optional int, clamp 0–600000), `incident_id` (optional str <=64; server mints one if absent). On validation
failure: clamp/drop the bad field and continue (an invalid client field must not reject a legitimate event),
except unknown `event_type` -> `{"action":"ignore"}` (safe now that the whitelist equals the emitted set).

---

## Decision 8 — Network monitoring & recovery

`server_monitor.py` is a standalone uptime emailer and is **not reused** as an in-app heartbeat. Instead the exam
page runs a `heartbeat_ms` (15s) `POST /heartbeat`; the server stores `last_heartbeat` and returns authoritative
remaining time. **New emitters (Finding 10):** `network_loss` and `device_disconnect` are **not** fired by the client today — they
are added in this change. The client gains `offline`/`online` handlers that fire a `network_loss` client event;
device loss is already covered by the existing `camera_off` emitter (its `ended`/`mute` track listeners), so
`device_disconnect` is used only as the **server-side** label when the heartbeat goes stale, not a new client type.
On `offline`/`online` the client shows a non-blocking banner, pauses nothing destructive,
and on reconnect **re-saves answers** (autosave already exists for whiteboards; extend to MCQ selections) and
re-reads the deadline from the heartbeat response. Prolonged loss (`now - last_heartbeat > server_timeout_s`) is
recorded by the server as a `network_loss`/`device_disconnect` event (`detection_source='server'`, never
auto-terminating) — the server staleness check is new and must be wired (there is no existing in-app heartbeat). Duplicate requests are
prevented by the submit guard + per-incident `incident_id` dedup.

---

## Decision 9 — Violation data model & dedup

Every event records (where applicable): `attempt_id` (-> candidate via join), `event_type`, `occurred_at`,
`duration_ms`, `severity`, `confidence` (nullable; set **only** for `device_detected` — a count like
`multiple_faces` stays null, Finding 4), `detection_source` (`client`/`server`/`model`),
`incident_id`, `snapshot_b64` (evidence ref), `review_status`, `admin_notes`; `warning_count` stays on the attempt.
**Timezone (Findings 1 & 8):** the ORM columns default to `datetime.utcnow`, but the authoritative attempt-timing
writes in the raw-PyMySQL paths use MySQL `NOW()` (session tz) — `started_at=NOW()` (line 1055), `submitted_at=NOW()`,
`terminated_at=IF(%s,NOW(),…)` (lines 1228-1229). The correct fix is **one consistent clock**, not a second one: all
**new** raw writes in this change (`occurred_at`, `last_heartbeat`) also use **`NOW()`**, and every elapsed-time
calculation (`/submit` deadline, `/heartbeat` `remaining_s`, prolonged-loss staleness) is computed **inside the DB
with `TIMESTAMPDIFF(SECOND, <ts>, NOW())`**. Because both operands come from `NOW()`, the math is correct regardless
of whether the session tz is UTC — this removes the earlier (incorrect) assumption that comparing against
`UTC_TIMESTAMP()` matched `started_at`'s stored semantics. (If the deployment later standardizes on UTC, migrate the
`started_at` insert to `UTC_TIMESTAMP()` and switch all four sites together — never mix the two.) **Dedup/continuity:** one continuous incident keeps one
`incident_id`; the server ignores repeats of the same `incident_id` inside `warning_cooldown_ms` and extends
`duration_ms` instead of inserting a new row — so one long look-away is a single incident, not a storm. Client vs
server separation is encoded in `detection_source`; the HR review endpoint (`/api/hr/attempt/<id>`) already does
`SELECT *` so the new columns surface in the dashboard with no query change — the admin sets `review_status` +
`admin_notes` (new `POST /api/hr/attempt/<id>/event/<event_id>/review`, `@role_required(HR_ROLES)`).
**`event_id` here is the `proctoring_events.id` primary key** (already present in the HR endpoint's `SELECT *`
rows), **not** the new `incident_id`; the HR template sends that `id` when the reviewer confirms/dismisses a row.

---

## Decision 10 — Warning & escalation engine

The rewritten `/violation` route replaces "warn on 1st, terminate on 2nd" with config-driven logic:
1. Validate + clamp input; verify ownership and `status=='In Progress'`.
2. Resolve/mint `incident_id`; if duplicate within `warning_cooldown_ms` -> extend duration, return `{"action":"noop"}`.
3. Insert `ProctoringEvent`; increment a **per-type** counter.
4. If per-type count hits `escalation.first_warning_at` -> `{"action":"warn", "message":...}` and set
   `first_warning_reason` once; the client shows a single warning (not every tick — cooldown enforces this).
5. If per-type count exceeds `max_violations[type]` **and** that type is not `-1` **and**
   `escalation.auto_action` is `auto_submit`/`terminate` **and** (`require_human_for_terminate` is False **or** the
   event is a hard, server-verified type) -> `{"action": auto_action, "reason":...}`. Uncertain AI types
   (`headphone_indicator`, `earphone_indicator`, `mouth_movement`, and single low-confidence `device_detected`)
   are `-1`/high-count and **never** trigger termination. On auto-submit/terminate the server preserves answers and writes `termination_reason`.
Different thresholds per type come straight from `max_violations`; cooldowns prevent duplicate warnings; defaults
are conservative; admins tune `PROCTOR_CFG` (future: a DB-backed override table, out of scope here).

---

## Files to change

- `Main.py` — add `PROCTOR_CFG` + `validate_proctor_cfg()`; extend `ProctoringEvent` + `AssessmentAttempt` models;
  add `_own_attempt()` helper; add `/precheck`, `/registered-photo`, `/heartbeat`, HR event-review routes; rewrite
  `/violation`; harden `/submit` (dup guard + UTC deadline); pass `proctor_cfg` to `take_assessment`.
- `migrations/0001_proctoring.sql` — new, idempotent additive migration (Decision 2).
- `templates/assessment_take.html` — consolidate the single `CFG` literal from `window.PROCTOR_CFG` (keep
  `IS_HANDHELD` multiplier); add identity capture into the existing pre-check and extend the `/precheck` body with
  `identity_snapshot`; add `offline`/`online` -> `network_loss` and the heartbeat `setInterval` + reconnect
  re-save; extend the existing `trigger()` to send `incident_id`/`duration_ms`/`confidence`. The
  copy/cut/paste/contextmenu/key restriction listeners already exist and are reused as-is.
- `templates/hr_assessments.html` — surface new columns (incident_id, confidence, source, review_status) + set
  `review_status`/`admin_notes`; show identity snapshot vs passport photo for human match.
- `templates/pre_check.html` — **out of scope for this change.** The authoritative pre-check is the in-page overlay
  inside `assessment_take.html` (that is where identity capture, noise calibration, and the `/precheck` POST live);
  the standalone `pre_check.html` is left untouched to avoid a second, divergent checklist. Revisit only if a
  separate standalone pre-check surface is later required, at which point it must share the same check-id set.

## Testability

- **Unit:** `validate_proctor_cfg()` (clamping/invalid input), the escalation decision function (pure: given
  type+counts+config -> action) factored out of the route so it is testable without a DB, input
  validation/clamping, UTC deadline computation, dedup window logic.
- **Integration:** `/precheck`/`/violation`/`/submit`/`/heartbeat` against a test MySQL (or SQLite shim for the
  ORM paths) — ownership 403s, duplicate-submit, deadline, cooldown dedup, auto-action gating.
- **Manual (unavoidably):** real camera/mic model behaviour, fullscreen/tab events across browsers, device-loss
  handling. Factoring the escalation logic out of the HTTP route is the key move that keeps the design testable.

## Edge cases

No passport photo (capture-only), iOS Safari (no fullscreen — already handled by `CAN_FULLSCREEN`), handheld slack
multiplier, model CDN failure (non-fatal path kept), mid-exam permission revocation (event, not auto-cheat),
duplicate submit, deadline passed while offline, concurrent tabs for same attempt (ownership + status guard),
clock skew (server is authoritative via heartbeat), bad/malicious client fields (clamped).

---

## Responses to design review (`.agents/design-review.json`, verdict CHANGES_REQUESTED)

All findings were re-verified against the live source before responding.

- **#1 [HIGH] event_type name mismatch — ADDRESSED.** `PROCTOR_CFG['max_violations']` now uses the exact strings
  the client's `trigger()` calls emit, verified in `assessment_take.html`: `tab_switch, window_blur,
  fullscreen_exit, second_display, devtools_open, meta_key, alt_tab, capture_shortcut, keyboard_shortcut,
  clipboard, no_face, multiple_faces, device_detected, eye_away, mouth_movement, noise, camera_off,
  camera_covered, feed_frozen` (+ the new indicator-only `headphone_indicator/earphone_indicator/
  device_disconnect/network_loss`). Decision 7 now states the server whitelist **is** the keys of that dict, so
  client and server derive from one list and the "unknown -> ignore" rule is safe. Dropped the invented
  `multiple_face/phone/gaze_away/head_turn` keys (gaze + look-down + head-turn all surface as `eye_away`; devices
  as `device_detected`).
- **#2 [HIGH] wrong client function — ADDRESSED.** Decision 4 is retargeted to the real
  `trigger(type, severity, desc)`: snapshot captured inside it, per-type `cd[type]`/`CFG.cooldownMs` reused, POST
  body `{event_type,severity,description,snapshot_b64}` extended with `incident_id`/`duration_ms`/`confidence`.
  No `sendViolation` reference remains.
- **#3 [HIGH] ADD COLUMN IF NOT EXISTS invalid on Oracle MySQL 8 — ADDRESSED.** The migration no longer uses
  `IF NOT EXISTS` on columns. Every column add is a guarded `information_schema.columns` dynamic-SQL block
  (same pattern as the indexes), so it runs on both Oracle MySQL 8.0 and MariaDB and stays additive-only.
- **#4 [MEDIUM] /precheck body — ADDRESSED.** Full body documented: `device_type, device_info, noise_baseline,
  passed, report` (the current client payload) **plus** `identity_snapshot` (newly added client-side here).
  `device_info`/`passed`/`noise_baseline` fold into `precheck_report` JSON; no extra columns.
- **#5 [MEDIUM] type mismatch / JSON serialization — ADDRESSED.** `identity_verified` is `SMALLINT` in SQL and
  `db.SmallInteger` in the model (agree). Added an explicit note that raw `/precheck`/`/heartbeat` paths
  `json.dumps(precheck_report)` and store `identity_snapshot` as a text data-URL.
- **#6 [MEDIUM] confidence mostly null — ADDRESSED.** `confidence` is nullable; populated only for `device_detected`
  (COCO `best.score`, a real 0–1 probability). All other events send `null`; server clamps 0–1 and accepts null.
  Stated in Decisions 4, the validation summary, and 9.
- **#7 [NIT] pre_check.html — ADDRESSED.** Declared **out of scope**; the in-page overlay is authoritative.
- **#8 [NIT] UTC claim — SUPERSEDED by second-cycle Finding #1 below.** The first-cycle attempt standardized new
  writes on `UTC_TIMESTAMP()`, which the second review correctly rejected (it mismatches `started_at`'s `NOW()`
  clock). Final resolution: everything uses `NOW()` + `TIMESTAMPDIFF`. See second-cycle #1.
- **#9 [NIT] event_id vs incident_id — ADDRESSED.** Stated that the review route's `event_id` is
  `proctoring_events.id`, not `incident_id`, and the HR template sends that id.
- **#10 [NIT] network_loss/device_disconnect not emitted today — ADDRESSED.** Flagged as new: client gains
  `offline`/`online` -> `network_loss`; `device_disconnect` is a server-side staleness label (device loss itself
  is already the existing `camera_off` emitter); the server heartbeat check is new and must be wired.

---

## Responses to design review — second cycle (`.agents/design-review.json`, verdict CHANGES_REQUESTED)

Re-verified every claim against the live source before editing (`Main.py` lines 1055, 1228-1229;
`assessment_take.html` lines 327-328, 1064). All four findings resolved.

- **#1 [MEDIUM] UTC deadline vs how `started_at` is written — ADDRESSED.** Confirmed `started_at` uses `NOW()`
  (session tz, `Main.py` line 1055), not `UTC_TIMESTAMP()`. The deadline/`remaining_s`/staleness math now computes
  elapsed time inside the DB with `TIMESTAMPDIFF(SECOND, started_at, NOW())`, so it is correct regardless of session
  tz. The new raw writes (`occurred_at`, `last_heartbeat`) and the existing `submitted_at`/`terminated_at` all stay
  on `NOW()` — one clock everywhere. The incorrect "matches `started_at`'s stored semantics via `UTC_TIMESTAMP()`"
  claim is removed (Decisions 7, 9; error table; migration comment).
- **#2 [MEDIUM] forked noise config — ADDRESSED.** `PROCTOR_CFG['noise']` is now `floor_db:18, margin_db:14,
  sustain_ms:1200` to match the live client (verified `assessment_take.html` line 1064). Decision 3 states the
  single derivation `CFG.noiseDb = Math.max(PROCTOR_CFG.noise.floor_db, baseline + PROCTOR_CFG.noise.margin_db)`,
  `CFG.noiseMs = PROCTOR_CFG.noise.sustain_ms`, and removes the dead static `noiseDb:26` literal. One source, no
  drift.
- **#3 [NIT] snapshot cap stated vaguely — ADDRESSED.** `identity_snapshot` and `snapshot_b64` both reference the
  exact shared constant `SNAPSHOT_MAX = 400000` (Decision 7).
- **#4 [NIT] confidence overloaded with a count — ADDRESSED.** `confidence` is restricted to `device_detected`
  only (COCO `best.score`). `multiple_faces` is a person count and stores `confidence=null`, with the count carried
  in `description` (Decisions 4, 9, config comment, validation summary).


---

## FINAL RESOLUTIONS (locked by orchestrator — design is APPROVED for implementation)

The design-loop reached 0 HIGH findings. The following resolve the last review items
(1 MEDIUM + 3 NITs from .agents/design-review.json). Implementation MUST follow these.

### R1 (resolves MEDIUM #1) — MCQ autosave endpoint
Adopt option (a). Add `POST /api/assessment/<int:attempt_id>/autosave`:
- Decorators/guards: `@login_required` + `@login_required_with_session` + `_own_attempt(attempt_id)` ownership check; require attempt `status == 'In Progress'` else return `{'ok': False, 'ignored': True}` (HTTP 200) without writing.
- Body: `{"answers": {"<question_id>": "<A|B|C|D>", ...}}`. Validate/clamp: keys must be ints that belong to this attempt's assessment; values must be one of A/B/C/D; ignore anything else.
- Persistence: add column `assessment_attempts.draft_answers` (JSON, nullable) via the SAME guarded information_schema migration. Upsert the full partial-answers JSON (last-write-wins). NO scoring, NO status change, NO finalization.
- `/submit` change: when the client POST body has missing/empty answers for a question, fall back to `draft_answers` so a reconnect/refresh never loses a selection. Scoring still happens only in `/submit`.
- Client (`assessment_take.html`): call `/autosave` on each answer selection (debounced ~1.5s) AND opportunistically on `visibilitychange`/`beforeunload`; also mirror to `localStorage` keyed by `attempt_id` as a belt-and-suspenders buffer that repopulates selections on reload if the server draft is unavailable. This is the "re-save answers on reconnect" flow — now concretely defined.

### R2 (resolves NIT #2) — session guard on new candidate routes
All new candidate-facing routes (`/precheck`, `/registered-photo`, `/heartbeat`, `/autosave`) use the full existing stack: `@login_required` + `@login_required_with_session` + `_own_attempt(attempt_id)`. Admin routes keep `@role_required(HR_ROLES)`.

### R3 (resolves NIT #3) — migration ordering + startup check
- Deploy note (put in final-summary.md): `migrations/0001_proctoring.sql` MUST be applied to MySQL BEFORE deploying the updated `Main.py`, because `db.create_all()` never ALTERs existing tables.
- Add a lightweight startup presence check in `Main.py` (run once at boot, wrapped in try/except, non-fatal): query `information_schema.columns` for one sentinel new column (e.g. `proctoring_events.incident_id`); if absent, `print()` a loud clear warning that the migration has not been applied. Do not crash the app; just warn.

### R4 (resolves NIT #4) — explicit detection_source on server inserts
`proctoring_events.detection_source` default stays `'client'` only for backward-compat of the existing path. EVERY server-originated insert (heartbeat-derived events, server-verified deadline/termination, network/device events the server records) MUST set `detection_source='server'` explicitly. Client-posted `/violation` rows are `'client'`.

### Honesty constraints (unchanged, carry into build)
Earbuds/AirPods/concealed phones/devtools: never auto-terminate on these; log as suspicious human-review indicators only. No speaker→mic tone gate. Keep uncertain AI predictions separate from confirmed misconduct. All migrations additive + idempotent + non-destructive; no invented columns/routes beyond those named here and in the design above.
