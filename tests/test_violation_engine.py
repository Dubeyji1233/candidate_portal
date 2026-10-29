"""Unit tests for the pure proctoring escalation decision function.

Run with the project venv (the one that satisfies requirements.txt):
    python -m pytest tests/test_violation_engine.py
or:
    python -m unittest tests.test_violation_engine

These tests exercise decide_violation_action(event_type, per_type_count, cfg), which is a
pure, DB-free function, so no database or network is required — only that Main.py imports
(i.e. Flask / Flask-SQLAlchemy / PyMySQL etc. are installed in the active interpreter).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Main import decide_violation_action, PROCTOR_CFG, HARD_SERVER_TYPES  # noqa: E402


def cfg_with(auto_action="warn_only", require_human=True):
    import copy
    c = copy.deepcopy(PROCTOR_CFG)
    c["escalation"]["auto_action"] = auto_action
    c["escalation"]["require_human_for_terminate"] = require_human
    return c


class TestDecideViolationAction(unittest.TestCase):
    def test_warn_only_default_never_escalates(self):
        cfg = cfg_with(auto_action="warn_only")
        # tab_switch limit is 3; even far over the ceiling, default policy only warns.
        self.assertEqual(decide_violation_action("tab_switch", 99, cfg), "warn")

    def test_first_warning_threshold(self):
        cfg = cfg_with()
        # Below first_warning_at would be noop, but first_warning_at defaults to 1.
        self.assertEqual(decide_violation_action("tab_switch", 1, cfg), "warn")

    def test_indicator_type_never_terminates(self):
        # earphone/headphone are -1 indicators: warn at most, even under terminate policy.
        cfg = cfg_with(auto_action="terminate", require_human=False)
        self.assertEqual(decide_violation_action("earphone_indicator", 50, cfg), "warn")
        self.assertEqual(decide_violation_action("headphone_indicator", 50, cfg), "warn")
        self.assertEqual(decide_violation_action("clipboard", 50, cfg), "warn")

    def test_uncertain_ai_never_terminates(self):
        # device_detected / eye_away / no_face etc. are uncertain AI predictions.
        cfg = cfg_with(auto_action="terminate", require_human=False)
        for t in ("device_detected", "eye_away", "no_face", "multiple_faces",
                  "mouth_movement", "camera_covered", "feed_frozen"):
            self.assertEqual(decide_violation_action(t, 99, cfg), "warn",
                             f"{t} must never escalate")

    def test_count_exceeded_requires_optin_auto_action(self):
        # Over ceiling but policy is warn_only -> still just warn.
        cfg = cfg_with(auto_action="warn_only")
        self.assertEqual(decide_violation_action("tab_switch", 10, cfg), "warn")

    def test_hard_server_type_escalates_under_optin(self):
        # tab_switch is a hard server type; over ceiling + auto_submit -> auto_submit.
        self.assertIn("tab_switch", HARD_SERVER_TYPES)
        cfg = cfg_with(auto_action="auto_submit", require_human=True)
        self.assertEqual(decide_violation_action("tab_switch", 10, cfg), "auto_submit")

    def test_require_human_blocks_non_hard_types(self):
        # keyboard_shortcut is NOT a hard server type; require_human blocks escalation.
        cfg = cfg_with(auto_action="terminate", require_human=True)
        self.assertEqual(decide_violation_action("keyboard_shortcut", 99, cfg), "warn")
        # With require_human False and over ceiling, it may terminate (keyboard_shortcut
        # is not an uncertain-AI type).
        cfg2 = cfg_with(auto_action="terminate", require_human=False)
        self.assertEqual(decide_violation_action("keyboard_shortcut", 99, cfg2), "terminate")

    def test_at_ceiling_warns_not_escalates(self):
        cfg = cfg_with(auto_action="auto_submit", require_human=False)
        # exactly at the ceiling (limit) -> warn, not escalate (escalation is for > limit).
        limit = PROCTOR_CFG["max_violations"]["second_display"]  # 2, a hard type
        self.assertEqual(decide_violation_action("second_display", limit, cfg), "warn")
        self.assertEqual(decide_violation_action("second_display", limit + 1, cfg), "auto_submit")


if __name__ == "__main__":
    unittest.main()
