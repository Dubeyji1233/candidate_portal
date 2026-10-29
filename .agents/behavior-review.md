# Proctoring module — behavior and correctness review

The change evolves the existing exam flow into a configurable proctoring layer: a per-type cooldown + incident de-dup + severity-weighted escalation engine with a conservative warn-only default, four new candidate routes (`/precheck`, `/registered-photo`, `/heartbeat`, `/autosave`), a hardened `/submit`, two new HR routes (per-event review, CSV export), a guarded additive migration, and a client that now derives its config from the server, mints stable incident ids, autosaves MCQ answers, heartbeats, and captures an identity snapshot. It is implemented in place (the workspace is not a git repo, so there is no diff to read — the review is against the on-disk files versus `.agents/proctoring-design.md` R1–R4 and `.agents/proctoring-plan.md`). The implementation is faithful to the locked design: the escalation decision is a pure function, all elapsed/deadline math is `TIMESTAMPDIFF(SECOND, started_at, NOW())`, every candidate attempt route is behind the IDOR helper, the migration is guarded/additive and mirrored in the ORM, and the honesty constraints (uncertain AI types never auto-terminate) hold.

Watch for: DB-dependent behaviors (incident de-dup, deadline, ownership 403s, autosave, heartbeat, CSV) have no runtime evidence — only `py_compile`, Jinja/`node --check` parse, and an AST-extracted logic check of the escalation function were run; the test suite that imports `Main` and all MySQL integration were explicitly not executed in this environment (possible). Client reload repopulates selections from `localStorage` only, never from the server `draft_answers`, so a resume on a different browser shows no prior answers even though scoring is still protected (possible).

**Verdict**: APPROVED

## High-level view

The escalation engine is split cleanly: `decide_violation_action(event_type, per_type_count, cfg)` is a pure, DB-free function returning `ignore|noop|warn|auto_submit|terminate`, and the `/violation` route does the I/O around it. The default `auto_action="warn_only"` means nothing auto-terminates out of the box, and the honesty rule is enforced in two independent places — `-1` indicator types and the `UNCERTAIN_AI_TYPES` set both short-circuit to `warn` regardless of count or policy, so AI-uncertain detections (device, gaze, faces, mouth, camera-covered, feed-frozen, earphones/headphones) can never end an exam.

Incident continuity is handled on both sides: the client keeps one `incident_id` for a continuous condition (rotating only after a recovery-quiet window that is independent of the send throttle) and grows `duration_ms`; the server de-dups repeats of the same `incident_id` inside `warning_cooldown_ms` by extending duration instead of inserting a new row. The per-type counter is `COUNT(DISTINCT incident_id)`, so one long look-away is one incident, not a storm.

All time math is anchored to one clock. `started_at`, `submitted_at`, `terminated_at`, `occurred_at`, and `last_heartbeat` are all `NOW()`, and every elapsed/deadline/remaining/staleness computation is `TIMESTAMPDIFF(SECOND, <ts>, NOW())` in the DB, so the result is correct regardless of session timezone. There is no `UTC_TIMESTAMP()` anywhere.

Security posture is solid for a change that touches a trust boundary. Every candidate attempt route goes through `_own_attempt()` (403 on mismatch), closing the prior IDOR hole; every HR route is `@role_required(HR_ROLES)`. The identity snapshot is validated as a decodable image data URL on write, stripped from the HR attempt JSON, and served only as raw bytes from an HR-only endpoint with a fixed content type and `nosniff`, so candidate-controlled text never crosses into HR markup. CSV export neutralizes spreadsheet formula injection and parameterizes every filter.

Finalization is a single idempotent path shared by `/submit` and the violation terminal branch: it clears prior answer rows then re-scores, runs inside an explicit transaction, and returns the terminal result only after commit (downgrading to `warn`/500 on failure so a terminal state never half-persists). `/submit` falls back to `draft_answers` for any missing selection (R1), never downgrades a Terminated attempt, and treats a no-answer resubmit as a duplicate.

The migration is guarded `information_schema` dynamic SQL per column and index, additive-only, flavor-portable, and includes `draft_answers`; every new column is mirrored in the ORM with matching types (`identity_verified` as `SmallInteger` for `SMALLINT`). Unrelated features (SSO, IceWarp/IMAP, profile, applications, requisitions, HR question CRUD, video review, whiteboard) are untouched, and exceptions are logged rather than swallowed. The remaining gaps are verification coverage and two client-side UX edges, none blocking.

<details>
<summary>Issues (5)</summary>

1. **DB runtime verification deferred** — incident de-dup, deadline, ownership 403s, autosave, heartbeat staleness, and CSV export have no executed evidence (no project venv, no MySQL in-env). Run the documented manual smoke tests and `python -m pytest tests/test_violation_engine.py` in the deploy-compatible venv before sign-off.
2. **Client reload uses localStorage only** — `restoreDraftSelections()` reads `localStorage`, never the server `draft_answers`, so resuming on a different browser/device shows no prior selections. Score safety still holds via the `/submit` server-side draft fallback; wire the server draft into the render if cross-device resume UX is wanted.
3. **Autosave drops option letters beyond A–D** — `/autosave` keeps only values in `('A','B','C','D')`. Any MCQ with more than four options would not persist those selections to the draft (the live `/submit` POST still captures them). Confirm no assessment uses >4 options, or widen the allowed set.
4. **warning_count semantics changed** — it now increments once per distinct-incident warn rather than only on the first warning. Intended under the new engine; confirm the HR dashboard reading of `warning_count` expects a running count.
5. **Second-granularity de-dup boundary** — `TIMESTAMPDIFF(SECOND,...)*1000` vs `warning_cooldown_ms` means a continuous incident whose POST lands just over the 4.5s window can occasionally produce a second row. Low impact; acceptable per design's second-granularity math.

</details>

<details>
<summary>Details</summary>

### Escalation engine: warn-only default and the two-layer honesty gate

`decide_violation_action` is pure and DB-free — the factoring that lets the escalation logic be unit-tested without a database. The ordering is correct: `-1` indicators and anything in `UNCERTAIN_AI_TYPES` return at most `warn` before policy is even consulted; a known-but-unlimited type warns; at/under the ceiling warns once `first_warning_at` is reached; only over the ceiling, under an opt-in `auto_submit`/`terminate`, and either `require_human_for_terminate` false or a `HARD_SERVER_TYPES` member, does it escalate (confirmed, Main.py decide_violation_action). The honesty constraint is enforced twice over — `device_detected`, `eye_away`, `no_face`, `multiple_faces`, `mouth_movement`, `camera_covered`, `feed_frozen`, and the earphone/headphone indicators are all in `UNCERTAIN_AI_TYPES`, so even `terminate` + `require_human=False` + count 99 yields `warn` (confirmed, matches the committed test cases).

The route wraps this with the full guard stack (`@login_required` + `@login_required_with_session` + `_own_attempt`), the `status=='In Progress'` gate returning `{'action':'ignore'}`, whitelist clamping, and — importantly — an unknown `event_type` is coerced to a bounded string, stored, logged, and returned as `ignore` rather than silently dropped (confirmed, Main.py assessment_violation). `confidence` is accepted and clamped to 0–1 only for `device_detected` and nulled otherwise; `duration_ms` clamps 0–600000; `snapshot_b64` caps at the shared `SNAPSHOT_MAX`.

One behavioral note worth surfacing: `warning_count` is incremented on every distinct-incident `warn`/`auto_submit`/`terminate`, not only on the first warning as the original route did (confirmed). `first_warning_reason` is still set once. This is consistent with the new "distinct warnings" model but is a change in what the counter means.

### Incident continuity across client and server

The client tracks incident *lifetime* separately from the send throttle: `incidentLastSeen[type]` plus `INCIDENT_RECOVERY_MS` (≥ `cooldownMs*2`) decides when a new `incident_id` is minted, while `cd[type]` only throttles POSTs. So a sustained condition keeps one id and a growing `duration_ms`, and the id rotates only after the detector has been quiet (confirmed, assessment_take.html trigger). The server mirrors this: a repeat of the same `incident_id` within `warning_cooldown_ms` extends `duration_ms` on the existing row and returns `{'action':'noop'}` with no new insert; the per-type counter is `COUNT(DISTINCT incident_id)` (confirmed). The de-dup age is computed at second granularity (`TIMESTAMPDIFF(SECOND,...)*1000` vs `warning_cooldown_ms`), so a POST that lands just over the 4.5s window can occasionally start a second row for the same continuous incident — low impact and in line with the design's second-granularity choice (possible).

### Submit hardening and the single finalizer

`_finalize_attempt` is the one scoring path for both `/submit` and the violation terminal branch. It deletes prior `assessment_answers` rows before re-inserting, so re-finalizing with a fuller set is idempotent and never doubles rows or inflates scores, and it writes the terminal status/score in the same statement set (confirmed, Main.py _finalize_attempt). Both callers wrap it in `conn.begin()/commit()` and only act on the terminal result after commit; on failure the violation branch downgrades to `warn` (keeping the attempt live with answers preserved in draft) and `/submit` returns 500 (confirmed). The proctoring-event insert happens before `begin()` under autocommit, so a rolled-back finalize still leaves the evidence row — the intended outcome.

`/submit` behavior matches the design's refinements: a resubmit of an already-finalized attempt with no new answers returns `{'duplicate':True}`; a resubmit carrying fresher answers re-scores (upgrade) but never downgrades a Terminated attempt to Completed; the deadline is `TIMESTAMPDIFF(SECOND, at.started_at, NOW()) > a.duration_minutes*60`, and when late it records `termination_reason='deadline exceeded'` while still accepting answers (confirmed). Missing/empty submitted answers fall back to `draft_answers` before scoring (R1), so a reconnect or refresh never loses a selection at scoring time.

### One clock everywhere

Verified beyond the high-level summary: a repo-wide search shows the only `UTC_TIMESTAMP` occurrence is a comment forbidding its use, and the two live computations (`/submit` deadline, `/heartbeat` `since_beat`/`remaining_s`) both read `TIMESTAMPDIFF(SECOND, <ts>, NOW())` against `NOW()`-written timestamps (confirmed, Main.py lines 1634 and 1790–1791).

### New candidate routes and the IDOR fix

`_own_attempt(attempt_id)` loads the row and returns it only when `candidate_email` matches `current_user.email` (case-insensitive), else `None` → `403` (confirmed, Main.py _own_attempt). It is applied to `/violation`, `/whiteboard`, `/submit`, `/precheck`, `/registered-photo`, `/heartbeat`, `/autosave`, `/record-chunk`, `/record-finish` — every candidate attempt route, closing the prior trust-the-URL hole (R2). `/precheck` folds `device_info`/`passed`/`noise_baseline` into a `json.dumps`'d `precheck_report` with a 16KB guard, sets `identity_verified=0`, and is fail-closed on the client (the exam starts only after `/precheck` returns `{ok:true}`). `/heartbeat` records a server-side `network_loss` row with `detection_source='server'` when the prior beat is older than `server_timeout_s`, and never auto-terminates (R4); `remaining_s` comes back on the same clock and the client reconciles its timer to it. `/autosave` is status-gated, validates keys against the attempt's assessment and values against A/B/C/D, and does last-write-wins JSON with no scoring or status change (confirmed).

The autosave value whitelist is correct given the radios render `value="{{ letter }}"`, but it silently drops any letter beyond A–D; an assessment with more than four options would not persist those selections to the draft, though the live `/submit` POST would still capture them (possible). Separately, `restoreDraftSelections()` repopulates only from `localStorage`, never from the server `draft_answers` — the belt-and-suspenders buffer works on the same browser but a cross-device resume shows no prior answers. The R1 correctness guarantee (no lost answers at scoring) still holds through the server-side `/submit` fallback, so this is a UX gap, not a scoring gap (possible).

### Identity snapshot: stored-XSS closed end to end

On write, `_parse_image_data_url` requires a `data:` URL with `;base64`, a mediatype in the jpeg/png/webp allowlist, and a payload that actually `b64decode(validate=True)`s; anything else is dropped and the prior valid snapshot is kept via `IFNULL` (confirmed, Main.py assessment_precheck + _parse_image_data_url). The HR attempt JSON strips `identity_snapshot`, `draft_answers`, and `precheck_report`, exposing only a `has_identity_snapshot` boolean; the bytes are served from an HR-only endpoint with a fixed image content type and `X-Content-Type-Options: nosniff`, and the HR page assigns `img.src` as a DOM property rather than interpolating markup (confirmed, api_hr_identity_snapshot + hr_assessments.html). Event text, incident ids, notes, and status are all `escapeHtml`/`escapeAttr`-escaped in the review modal.

### CSV export

Filters (candidate, exam, date range, severity, review_status) are all bound parameters with no string interpolation, severity/review_status are enum-checked, and `_csv_safe` prefixes any cell starting with `= + - @` or a leading tab/CR with a single quote to neutralize spreadsheet formula injection (confirmed, api_hr_attempts_export). Output is UTF-8-BOM CSV via the stdlib `csv` module.

### Client config consolidation

`window.PROCTOR_CFG = {{ proctor_cfg|tojson }}` is injected and `CFG` is rebuilt from it with `HANDHELD_SLACK` kept as a multiplier. `noiseMs` is `PCFG.noise.sustain_ms` (1200) and, at calibration, `CFG.noiseDb = Math.max(PCFG.noise.floor_db, noiseBaseline + PCFG.noise.margin_db)` with `floor_db:18`/`margin_db:14` from the server block; the dead static `noiseDb:26` is gone (confirmed; grep finds no `noiseDb:26`). The derived values are byte-for-byte the previous live numbers, so behavior is unchanged while the config stops being forked. `trigger()` sends `incident_id`/`duration_ms`/`confidence`, with `confidence` numeric only for `device_detected` and null elsewhere, and both `terminate` and `auto_submit` are treated as terminal (stop recording, post freshest answers, navigate after server confirmation).

### Migration and model mirror

`0001_proctoring.sql` adds every column (`proctoring_events`: `incident_id`, `duration_ms`, `confidence`, `detection_source`, `review_status`, `admin_notes`; `assessment_attempts`: `identity_verified`, `identity_snapshot` LONGTEXT, `precheck_report` JSON, `last_heartbeat`, `draft_answers` JSON) and both indexes via guarded `information_schema` dynamic SQL — no `ADD COLUMN IF NOT EXISTS`, no drop/recreate, re-runnable on both Oracle MySQL 8 and MariaDB (confirmed). The ORM mirrors all of them with matching types, notably `identity_verified = db.SmallInteger` for `SMALLINT` (confirmed, Main.py models). The R3 non-fatal startup presence check (`_check_proctoring_migration` via idempotent `run_startup_checks`) fires at both module import and `__main__`, so a WSGI deployment also gets the warning.

### Preserved features and error handling

SSO (`/candidate-sso-login`, `/getAToken`), IceWarp/IMAP login, profile, applications, requisitions, HR question CRUD (add/edit/delete), the existing attempt-review endpoint, video record/finish, and whiteboard autosave are all present and unchanged in behavior (confirmed from the route inventory). New routes log exceptions via `print()` and return explicit error JSON rather than swallowing them.

### Test coverage and verification gaps

Covered by executed evidence: `python -m py_compile Main.py` and the test module (OK), Jinja parse of both templates and `node --check` of the extracted scripts (OK), and an AST-extracted exercise of `decide_violation_action`/`PROCTOR_CFG`/`HARD_SERVER_TYPES`/`UNCERTAIN_AI_TYPES` against the committed assertions plus whitelist-coverage (21/21). The committed `tests/test_violation_engine.py` covers warn-only default, first-warning threshold, `-1` and uncertain-AI never terminating, opt-in-gated escalation, hard-server-type escalation, `require_human` blocking non-hard types, and at-vs-over ceiling.

Not executed (environment lacks the project venv and MySQL, documented honestly): `import Main` and therefore `pytest` of the real test module; and all DB-dependent behaviors — migration idempotency re-run, ownership 403s, incident de-dup, deadline, autosave persistence, heartbeat staleness, and CSV export. These read as correct but are confirmed only by inspection, so they must be smoke-tested in the deploy-compatible environment before final sign-off (possible).

</details>

<details>
<summary>File map</summary>

- `Main.py` — `PROCTOR_CFG` + `SNAPSHOT_MAX` + `validate_proctor_cfg`; `HARD_SERVER_TYPES`/`UNCERTAIN_AI_TYPES`; pure `decide_violation_action`; `_own_attempt` IDOR helper; `_parse_image_data_url`; `_check_proctoring_migration`/`run_startup_checks`; ORM column adds on `ProctoringEvent`/`AssessmentAttempt`; `_load_draft` + `_finalize_attempt`; rewritten `/violation`; hardened `/submit`; new `/precheck`, `/registered-photo`, `/heartbeat`, `/autosave`; HR `/identity-snapshot`, `/registered-photo`, `/event/<id>/review`, `/attempts/export`.
- `migrations/0001_proctoring.sql` — guarded additive migration (6 event columns, 5 attempt columns incl. `draft_answers`, 2 indexes).
- `templates/assessment_take.html` — `window.PROCTOR_CFG` injection + `CFG` derivation; `trigger()` incident id/duration/confidence; autosave (debounced + beacon + localStorage); heartbeat; offline/online → `network_loss`; identity capture + `/registered-photo`; fail-closed Start handler.
- `templates/hr_assessments.html` — per-event incident/confidence/source/review_status + notes + review POST; identity vs passport side-by-side via DOM `img.src`; filter inputs + Export CSV.
- `tests/test_violation_engine.py` — unit tests for the pure escalation function.

No git diff available (workspace is not a git repository); review is against the on-disk files.

</details>
