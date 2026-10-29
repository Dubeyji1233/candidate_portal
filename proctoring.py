"""
Server-side proctoring helpers.
- Classifies client events against the central config.
- Deduplicates continuous incidents by (attempt_id, incident_key, window).
- Applies escalation policy.
- Writes evidence to disk (never trust client b64 beyond size limits).
"""
from __future__ import annotations
import base64, hashlib, os, re, time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from proctoring_config import PROCTOR

EVIDENCE_DIR = Path(os.path.dirname(__file__)) / "evidence"
EVIDENCE_DIR.mkdir(exist_ok=True)


# ── identity / dedupe ─────────────────────────────────────────────────────

def incident_key(attempt_id: int, event_type: str, description: str = "") -> str:
    raw = f"{attempt_id}|{event_type}|{description[:60]}".encode()
    return hashlib.sha1(raw).hexdigest()[:20]


def chunk_key_hash(attempt_id: int, chunk_index, chunk_bytes: bytes = b"") -> str:
    h = hashlib.sha256()
    h.update(str(attempt_id).encode()); h.update(b"|")
    h.update(str(chunk_index).encode()); h.update(b"|")
    if isinstance(chunk_bytes, (bytes, bytearray)):
        h.update(bytes(chunk_bytes))
    return h.hexdigest()[:32]


# ── policy / state machine ────────────────────────────────────────────────

def classify(event_type: str) -> Tuple[str, str]:
    rule = PROCTOR.rule_for(event_type)
    return rule["severity"].capitalize(), rule["action"]


def policy_action(event_type: str) -> str:
    return PROCTOR.rule_for(event_type)["action"]


def next_phase(current_state, incoming_phase):
    incoming_phase = (incoming_phase or "started").strip().lower()
    if current_state is None:
        return "started" if incoming_phase == "started" else None
    if current_state == "ended":
        return "ended"
    if incoming_phase == "ended":
        return "ended"
    if incoming_phase in ("updated", "started"):
        return "ongoing"
    return current_state


def should_dedupe(last_event_dt: Optional[datetime], cooldown_ms: int) -> bool:
    if not last_event_dt:
        return False
    return (datetime.utcnow() - last_event_dt) < timedelta(milliseconds=cooldown_ms)


# ── evidence / time ───────────────────────────────────────────────────────

def save_evidence(attempt_id: int, event_id: int, snapshot_b64: str) -> Optional[str]:
    if not PROCTOR.capture_evidence or not snapshot_b64:
        return None
    if len(snapshot_b64) > PROCTOR.max_snapshot_bytes:
        return None
    try:
        if "," in snapshot_b64:
            snapshot_b64 = snapshot_b64.split(",", 1)[1]
        raw = base64.b64decode(snapshot_b64, validate=True)
    except Exception:
        return None
    if len(raw) > PROCTOR.max_snapshot_bytes:
        return None
    subdir = EVIDENCE_DIR / f"attempt_{attempt_id}"
    subdir.mkdir(exist_ok=True)
    fname = f"event_{event_id}_{int(time.time())}.jpg"
    (subdir / fname).write_bytes(raw)
    return f"attempt_{attempt_id}/{fname}"


def is_attempt_expired(deadline_at: Optional[datetime]) -> bool:
    return bool(deadline_at and datetime.utcnow() > deadline_at)


# ── validation ────────────────────────────────────────────────────────────

_ALLOWED_PHASES = {"started", "updated", "ended"}
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def validate_incident(payload):
    if not isinstance(payload, dict):
        return None, "invalid_payload"

    event_type = (payload.get("event_type") or "").strip()
    if not event_type or len(event_type) > 50:
        return None, "invalid_event_type"

    phase = (payload.get("phase") or "started").strip().lower()
    if phase not in _ALLOWED_PHASES:
        return None, "invalid_phase"

    severity_raw = (payload.get("severity") or "").strip()
    description  = (payload.get("description") or "")[:500]

    source = (payload.get("source") or "client").strip().lower()
    if source not in ("client", "server"):
        source = "client"

    incident_id = (payload.get("incident_id") or "").strip()
    if not incident_id:
        incident_id = hashlib.sha1(
            f"{event_type}|{description[:60]}".encode()
        ).hexdigest()[:20]

    try:
        duration_ms = max(0, int(payload.get("duration_ms") or 0))
    except (TypeError, ValueError):
        duration_ms = 0

    confidence = payload.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None

    snapshot_b64 = payload.get("snapshot_b64")
    if snapshot_b64 is not None and not isinstance(snapshot_b64, str):
        snapshot_b64 = None
    if isinstance(snapshot_b64, str) and len(snapshot_b64) > PROCTOR.max_snapshot_bytes:
        snapshot_b64 = None

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    return {
        "event_type":   event_type,
        "severity":     severity_raw or None,
        "source":       source,
        "description":  description,
        "phase":        phase,
        "incident_id":  incident_id,
        "duration_ms":  duration_ms,
        "confidence":   confidence,
        "snapshot_b64": snapshot_b64,
        "metadata":     metadata,
    }, None


def validate_identity_snapshot(snapshot_b64) -> bool:
    if not isinstance(snapshot_b64, str) or not snapshot_b64:
        return False
    if len(snapshot_b64) > PROCTOR.max_snapshot_bytes:
        return False
    body = snapshot_b64.split(",", 1)[1] if "," in snapshot_b64 else snapshot_b64
    if len(body) < 100 or len(body) % 4 != 0 or not _B64_RE.match(body):
        return False
    try:
        raw = base64.b64decode(body, validate=True)
    except Exception:
        return False
    return raw[:2] in (b"\xff\xd8", b"\x89P") or raw[:3] == b"GIF"


def validate_recording_chunk(chunk_bytes, *, max_bytes=None) -> bool:
    if not isinstance(chunk_bytes, (bytes, bytearray)) or not chunk_bytes:
        return False
    if max_bytes and len(chunk_bytes) > max_bytes:
        return False
    return True


def validate_precheck(report: dict, is_sample: bool = False) -> Tuple[bool, str]:
    if not isinstance(report, dict):
        return False, "invalid_report"

    failed = [k for k, v in report.items()
              if isinstance(v, dict) and v.get("status") == "fail"]

    soft = {"screenrec", "fullscreen", "earphones", "noise", "device"}
    if is_sample:
        soft = soft | {"browser", "network", "camera", "lighting", "face",
                       "objects", "audio", "displays", "microphone", "screen"}

    hard = [k for k in failed if k not in soft]
    if hard:
        return False, "failed:" + ",".join(hard)
    return True, "ok"


# ── summary ───────────────────────────────────────────────────────────────

def build_summary(rows):
    rows = list(rows or [])

    def _g(r, key, default=None):
        try:
            v = r[key]
        except (KeyError, TypeError, IndexError):
            v = getattr(r, key, default)
        return v if v is not None else default

    by_type, by_source, by_state = Counter(), Counter(), Counter()
    total_ms = 0
    max_ms   = 0

    for r in rows:
        et  = str(_g(r, "event_type")  or "unknown")
        src = str(_g(r, "source")      or "unknown")
        st  = str(_g(r, "event_state") or "ended")
        try:
            dur = max(0, int(_g(r, "duration_ms") or 0))
        except (TypeError, ValueError):
            dur = 0

        by_type[et]   += 1
        by_source[src] += 1
        by_state[st]   += 1
        total_ms      += dur
        if dur > max_ms:
            max_ms = dur

    return {
        "total_events":      len(rows),
        "total_duration_ms": total_ms,
        "max_duration_ms":   max_ms,
        "by_type":           dict(by_type),
        "by_source":         dict(by_source),
        "by_state":          dict(by_state),
        "distinct_types":    len(by_type),
    }