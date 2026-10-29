"""
Centralised proctoring configuration.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Dict


def _env_int(name, default, lo=None, hi=None):
    raw = os.getenv(name)
    try:
        v = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo: v = lo
    if hi is not None and v > hi: v = hi
    return v


def _env_float(name, default, lo=None, hi=None):
    raw = os.getenv(name)
    try:
        v = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo: v = lo
    if hi is not None and v > hi: v = hi
    return v


def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None: return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ProctoringConfig:
    # ----- lifecycle -----
    grace_period_ms: int = field(default_factory=lambda: _env_int("PC_GRACE_MS", 8000, 0, 60000))
    camera_interval_ms: int = field(default_factory=lambda: _env_int("PC_CAM_MS", 600, 300, 5000))

    # ----- face -----
    face_away_ms: int = field(default_factory=lambda: _env_int("PC_FACE_AWAY_MS", 1800, 500, 20000))
    multi_face_ms: int = field(default_factory=lambda: _env_int("PC_MULTI_FACE_MS", 1200, 500, 20000))
    face_offset_limit: float = field(default_factory=lambda: _env_float("PC_FACE_OFFSET", 0.20, 0.05, 0.5))

    # ----- gaze / head -----
    gaze_away_ms: int = field(default_factory=lambda: _env_int("PC_GAZE_MS", 1500, 300, 20000))
    head_turn_deg: float = field(default_factory=lambda: _env_float("PC_HEAD_DEG", 18, 5, 60))
    look_down_ratio: float = field(default_factory=lambda: _env_float("PC_LOOKDOWN", 0.85, 0.3, 1.5))

    # ----- objects -----
    phone_confidence: float = field(default_factory=lambda: _env_float("PC_PHONE_CONF", 0.35, 0.05, 1.0))
    device_confidence: float = field(default_factory=lambda: _env_float("PC_DEVICE_CONF", 0.45, 0.05, 1.0))
    phone_sustain_ms: int = field(default_factory=lambda: _env_int("PC_PHONE_MS", 1000, 200, 20000))

    # ----- audio -----
    noise_margin_db: float = field(default_factory=lambda: _env_float("PC_NOISE_MARGIN", 14.0, 3.0, 40.0))
    noise_sustain_ms: int = field(default_factory=lambda: _env_int("PC_NOISE_MS", 1200, 200, 20000))
    noise_floor_db: float = field(default_factory=lambda: _env_float("PC_NOISE_FLOOR", 18.0, 5.0, 60.0))

    # ----- escalation -----
    warning_cooldown_ms: int = field(default_factory=lambda: _env_int("PC_COOLDOWN_MS", 6000, 500, 120000))
    # ═══ FIX 7: Allow up to 2 warnings (terminates on 3rd strike) ═══
    max_warnings: int = field(default_factory=lambda: _env_int("PC_MAX_WARN", 2, 0, 10))
    auto_terminate_on_hard: bool = field(default_factory=lambda: _env_bool("PC_AUTO_TERM_HARD", True))

    # ----- network -----
    heartbeat_interval_ms: int = field(default_factory=lambda: _env_int("PC_HB_MS", 15000, 3000, 120000))
    heartbeat_timeout_ms: int = field(default_factory=lambda: _env_int("PC_HB_TIMEOUT", 45000, 10000, 600000))

    # ----- evidence -----
    capture_evidence: bool = field(default_factory=lambda: _env_bool("PC_EVIDENCE", True))
    evidence_retention_days: int = field(default_factory=lambda: _env_int("PC_EVIDENCE_DAYS", 90, 1, 3650))
    max_snapshot_bytes: int = field(default_factory=lambda: _env_int("PC_SNAP_BYTES", 400_000, 50_000, 5_000_000))

    # In ProctoringConfig dataclass, update the rule_table:
    rule_table: Dict[str, dict] = field(default_factory=lambda: {
        "multiple_faces": {"severity": "high", "action": "warn_then_terminate"},  # CHANGED
        "no_face": {"severity": "high", "action": "warn_then_terminate"},
        "eye_away": {"severity": "high", "action": "warn_then_terminate"},
        "head_turn": {"severity": "high", "action": "warn_then_terminate"},
        "device_detected": {"severity": "high", "action": "warn_then_terminate"},
        "earphone_visible": {"severity": "high", "action": "warn_then_terminate"},
        "hand_to_ear": {"severity": "low", "action": "log_only"},
        "noise": {"severity": "low", "action": "warn_then_terminate"},
        "mouth_movement": {"severity": "low", "action": "log_only"},
        "tab_switch": {"severity": "high", "action": "warn_then_terminate"},
        "window_blur": {"severity": "high", "action": "warn_then_terminate"},
        "fullscreen_exit": {"severity": "high", "action": "warn_then_terminate"},
        "second_display": {"severity": "high", "action": "warn_then_terminate"},
        "devtools_open": {"severity": "high", "action": "warn_then_terminate"},
        "clipboard": {"severity": "medium", "action": "log_only"},
        "keyboard_shortcut": {"severity": "medium", "action": "log_only"},
        "camera_off": {"severity": "high", "action": "warn_then_terminate"},
        "camera_covered": {"severity": "high", "action": "warn_then_terminate"},
        "feed_frozen": {"severity": "high", "action": "warn_then_terminate"},
        "precheck_failed": {"severity": "critical", "action": "terminate"},
        "network_lost": {"severity": "medium", "action": "log_only"},
        "manual_terminate": {"severity": "critical", "action": "terminate"},
    })

    def rule_for(self, event_type: str) -> dict:
        return self.rule_table.get(event_type,
                {"severity": "medium", "action": "warn_then_terminate"})


PROCTOR = ProctoringConfig()


def public_config() -> dict:
    """Subset exposed to the browser — thresholds used by the JS detectors."""
    return {
        "grace_period_ms":       PROCTOR.grace_period_ms,
        "camera_interval_ms":    PROCTOR.camera_interval_ms,
        "face_away_ms":          PROCTOR.face_away_ms,
        "multi_face_ms":         PROCTOR.multi_face_ms,
        "gaze_away_ms":          PROCTOR.gaze_away_ms,
        "head_turn_deg":         PROCTOR.head_turn_deg,
        "look_down_ratio":       PROCTOR.look_down_ratio,
        "phone_confidence":      PROCTOR.phone_confidence,
        "device_confidence":     PROCTOR.device_confidence,
        "phone_sustain_ms":      PROCTOR.phone_sustain_ms,
        "noise_margin_db":       PROCTOR.noise_margin_db,
        "noise_floor_db":        PROCTOR.noise_floor_db,
        "noise_sustain_ms":      PROCTOR.noise_sustain_ms,
        "warning_cooldown_ms":   PROCTOR.warning_cooldown_ms,
        "max_warnings":          PROCTOR.max_warnings,
        "heartbeat_interval_ms": PROCTOR.heartbeat_interval_ms,
        "evidence_enabled":      PROCTOR.capture_evidence,
        "face_offset_limit":     PROCTOR.face_offset_limit,
    }