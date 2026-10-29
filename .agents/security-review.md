# Config-driven proctoring with identity evidence and autosaved finalization

The module adds candidate-owned precheck, heartbeat, autosave, photo, violation, and recording flows; an HR-only review/export surface; and a guarded additive migration. Server policy, source labels, nullable confidence, and privileged image serving follow the approved shape. Terminal-state and evidence-lifecycle behavior still diverges from the locked design, and recorded verification omits the DB-backed checks that establish the security boundary. Watch for: **confirmed** post-result score rewrites; **confirmed** missing terminal-submit acknowledgement; **confirmed** configurable devtools auto-termination; **confirmed** mutable or incomplete review evidence; **confirmed** fragmented incidents; and **confirmed** missing runtime verification.

**Verdict**: NEEDS_CHANGES

## High-level view

Candidate proctoring APIs carry both authentication decorators and `_own_attempt`; older result/take paths use equivalent candidate-email predicates. Registered-photo serves `current_user.passport_photo`, and HR evidence, video, review, and CSV routes are role-gated server-side. The remaining trust-boundary gap is temporal: `/precheck` lets an owner replace identity evidence and reset verification after finalization.

The terminal workflow finalizes from the server draft and then permits any non-empty follow-up submission to replace stored answers and score. There is no one-time handoff or in-progress guard, and the browser redirects whether that follow-up succeeds or fails.

Model confidence remains separate from review status, unknown event types are logged and stored with an ignore action, server/client insert sources are explicit, and multiple-face confidence remains null. Two honesty/integrity gaps remain: the devtools geometry heuristic can terminate under a supported configuration, and the ear challenge produces no evidence available to human reviewers. Stable incident IDs also generate repeated rows after the initial cooldown.

The migration is guarded and additive, and both snapshot fields use the 400,000-character cap. Recorded checks cover compilation, template/JavaScript parsing, pure escalation assertions, and static whitelist matching, but explicitly omit migration rerun, authorization responses, transactional finalization, dedup, deadline, autosave, heartbeat, and CSV execution.

<details>
<summary>Issues (7)</summary>

1. **Finalized-attempt score rewrite** — **confirmed**: a candidate can submit non-empty answers after completion or termination and replace the stored answers and score. Restore an atomic `status == 'In Progress'` guard and solve terminal answer freshness before the status transition.
2. **Terminal answer acknowledgement gap** — **confirmed**: the browser navigates after `/submit` regardless of HTTP/JSON success, so answers newer than the server draft can be lost. Remain in a retryable terminal-pending state until the server confirms the current answer set.
3. **Devtools auto-termination** — **confirmed**: `devtools_open` can terminate when automatic action is enabled and the human gate is disabled. Make it indicator-only and invariantly never-terminal.
4. **Mutable precheck evidence** — **confirmed**: an owner can replace identity/precheck evidence and reset `identity_verified` after finalization. Status-gate and make accepted evidence immutable except through a privileged recheck.
5. **Discarded ear-review evidence** — **confirmed**: ear-view frames are cleared without persistence, the output-device-label indicator is absent, and HR cannot read the stored precheck result. Persist and expose bounded review-only evidence without treating it as conclusive misconduct.
6. **Continuous-incident row fragmentation** — **confirmed**: repeats after one cooldown create additional rows with the same incident ID instead of extending one row. Deduplicate by stable incident identity or maintain a distinct server last-seen window.
7. **Missing DB-backed verification** — **confirmed**: the recorded evidence says the migration, authorization, transaction, deadline, dedup, autosave, heartbeat, and CSV checks were not run. Execute and attach those checks in a deployment-compatible venv against disposable MySQL before sign-off.

</details>

<details><summary>Details</summary>

## Finalized attempts can be re-scored by the candidate

**confirmed** — `assessment_submit` treats `Completed` and `Terminated` attempts as duplicates only when the request has no answers (`Main.py:1592-1683`). With any non-empty map, `_finalize_attempt` deletes existing answer rows, stores the new choices, recalculates the score, and commits against the already-final attempt (`Main.py:1364-1427`). A candidate can inspect a result and repeatedly POST revised choices; preserving `Terminated` status does not protect answer or score integrity.

The locked design requires every `status != 'In Progress'` request to return the existing result without re-scoring. Enforce that condition inside the finalization transaction. Fresh terminal answers must be bound to the still-in-progress transition, such as by synchronously saving current selections before requesting the terminal action, rather than reopening the result.

## Terminal navigation does not confirm current answers were stored

**confirmed** — after `/violation` returns `auto_submit` or `terminate`, `trigger()` invokes `submitTest()` with current selections (`assessment_take.html:1584-1644`). `submitTest()` ignores the `/submit` status/body, catches network failure, and always navigates (`assessment_take.html:1513-1540`). The violation transaction confirms only `draft_answers`; a selection inside the 1.5-second debounce can therefore be absent, and failure of the follow-up loses it from the persisted result.

Keep a recoverable terminal-pending page until `/submit` returns `{ok:true}`, retry from local storage, and navigate only after confirmation. This must be paired with the duplicate guard so freshness cannot become a general post-result rewrite capability.

## Devtools geometry can become an automatic misconduct verdict

**confirmed** — the client cautiously reports developer tools that “appear to be open” from outer/inner window gaps (`assessment_take.html:1688-1703`). The server gives `devtools_open` a finite threshold and omits it from `UNCERTAIN_AI_TYPES` (`Main.py:80-107`), so `auto_submit` or `terminate` escalates it when `require_human_for_terminate=False` (`Main.py:139-178`). This violates the locked rule that devtools detection remains a human-review indicator and never auto-terminates.

Set its threshold to `-1` and include it in the explicit never-terminal set so the guarantee survives configuration changes.

## Candidate-owned precheck evidence remains mutable after finalization

**confirmed** — `/precheck` checks ownership but not status, then updates `precheck_report`, replaces `identity_snapshot` when another valid data URL is supplied, and resets `identity_verified=0` (`Main.py:1686-1757`). An owner can alter the audit record after completion/termination and undo a human verification value.

Reject writes outside the permitted attempt phase and prevent replacement after evidence acceptance except through a privileged recheck. Put the status/immutability predicate in the update so it cannot race finalization.

## Ear-review evidence is discarded

**confirmed** — the challenge pushes canvases into `challengeFrames`, never serializes or posts them, and clears them in `finally` (`assessment_take.html:490-599`). The approved audio-output `enumerateDevices()` indicator is absent. Although a precheck report is stored, `api_hr_attempt` removes it and the HR UI has no alternate access (`Main.py:2074-2084`), leaving reviewers without the challenge evidence or status.

Persist size-bounded ear-view/output-label evidence behind HR authorization and keep it non-conclusive and non-terminal. Reducing the feature to self-attestation requires an explicit design change.

## Stable incidents create repeated evidence rows

**confirmed** — the client retains an incident ID and sends again when its 4,500 ms cooldown expires (`assessment_take.html:1584-1629`). The server compares repeats with the original row’s unchanged `occurred_at` and deduplicates only while age is strictly below the same threshold (`Main.py:1495-1510`), so sends at or after the cooldown create more rows with the same ID. Distinct-ID counting avoids escalation inflation, but duration/evidence is fragmented.

Update the row whenever the stable incident ID already exists, or maintain a separate server last-seen recovery window. Cover a repeat crossing the cooldown in DB-backed verification.

## Database-backed verification is absent

**confirmed** — `.agents/proctoring-plan.md` records compile, parse, JavaScript syntax, pure decision, and static whitelist success, then states that migration idempotency, ownership 403s, dedup, deadlines, autosave, heartbeat, CSV, and DB finalization were not executed. Static inspection cannot establish deployed schema compatibility, HTTP authorization outcomes, or transaction behavior.

Run two migration passes plus owned/non-owned candidate requests, non-HR denials, concurrent/duplicate terminal submissions, terminal answer preservation, heartbeat source labels, and cooldown-crossing dedup against disposable MySQL. This review did not rerun suites, as required.

</details>

<details>
<summary>File map</summary>

- `Main.py` — policy, ownership, event escalation/storage, scoring, candidate evidence APIs, and HR review/export.
- `templates/assessment_take.html` — precheck, detectors, autosave/heartbeat, incident reporting, recording, and submission.
- `templates/hr_assessments.html` — HR evidence review, identity comparison, incident disposition, filters, and CSV launch.
- `migrations/0001_proctoring.sql` — guarded additive proctoring columns and incident indexes.

Full diff: unavailable because the workspace has no `.git` metadata; scope is the four in-place files named by the task and the approved plan’s implementation anchors.

</details>
