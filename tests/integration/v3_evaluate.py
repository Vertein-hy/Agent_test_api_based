"""Repeatable longitudinal regression suite: days simulated, not claimed lived.
Runs on fictional graph records; emits aggregate pass/fail without chat content.
"""

import unittest
from pathlib import Path
import os
import json
import time
from test_memory_v3 import CrossContextGraphTests, LearningLifecycleTests
from test_policy_jobs import PolicyTests, JobTests

suite = unittest.TestSuite(
    unittest.defaultTestLoader.loadTestsFromTestCase(c)
    for c in (CrossContextGraphTests, LearningLifecycleTests, PolicyTests, JobTests)
)
result = unittest.TextTestRunner(verbosity=1).run(suite)
report = {
    "timestamp": time.time(),
    "tests": result.testsRun,
    "failures": len(result.failures),
    "errors": len(result.errors),
    "passed": result.wasSuccessful(),
    "data": "fictional_only",
    "simulated_horizon_days": [14, 90, 900],
    "checks": [
        "same_account_across_contexts",
        "other_account_isolation",
        "private_audience",
        "latest_name",
        "revocation",
        "late_job_no_resurrection",
        "task_completion",
        "retention_decay",
        "pinning",
        "explicit_feedback_learning",
        "forget_clears_learning",
        "restart_persistence",
    ],
    "limitation": "Simulated regression is not multiweek real-chat accuracy.",
}
(Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "v3-evaluation.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2)
)
print(json.dumps(report, ensure_ascii=False))
raise SystemExit(0 if result.wasSuccessful() else 1)
