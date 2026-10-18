#!/usr/bin/env python3
"""WorkGrid — Phase 9 Integration and Security Test Runner.

Executes Phase 9 integration and security test suites against the isolated,
disposable 'workgrid_test' database with safety confirmations.

Usage:
    python scripts/run_phase9_tests.py
    python scripts/run_phase9_tests.py --all
    python scripts/run_phase9_tests.py -k test_concurrent
"""
import os
import subprocess
import sys
from pathlib import Path

# Ensure execution in backend directory
BACKEND_DIR = Path(__file__).resolve().parents[1]

# Set isolated test database environment variables
env = os.environ.copy()
env["WORKGRID_TEST_DATABASE_URL"] = os.environ.get(
    "WORKGRID_TEST_DATABASE_URL",
    "postgresql://workgrid_test:workgrid_test@localhost:5432/workgrid_test",
)
env["WORKGRID_TEST_DATABASE_CONFIRM"] = "I_HAVE_VERIFIED_THIS_IS_DISPOSABLE"
env["WORKGRID_TEST_DATABASE_NAME"] = "workgrid_test"
env["WORKGRID_TEST_DATABASE_USER"] = "workgrid_test"
env["APP_ENV"] = "testing"
env["ENABLE_DEMO_LOGIN"] = "true"
env["PYTHONPATH"] = str(BACKEND_DIR)

# Determine arguments
args = sys.argv[1:]
if "--all" in args:
    args.remove("--all")
    test_target = ["tests/"]
else:
    test_target = [
        "tests/test_phase9_authorization_boundaries.py",
        "tests/test_phase9_concurrency_and_stale_edits.py",
        "tests/test_phase9_idempotency_and_history.py",
        "tests/test_phase9_workflow_transitions.py",
        "tests/test_phase9_worker_and_retries.py",
        "tests/test_phase9_dashboard_and_ordering.py",
        "tests/test_phase9_sse_security.py",
        "tests/test_phase9_end_to_end_flows.py",
    ]

cmd = [sys.executable, "-m", "pytest"] + test_target + ["-vv", "--tb=short"] + args

print("=" * 70)
print("WorkGrid Phase 9 — Integration & Security Test Suite")
print("Target DB: workgrid_test (Isolated Disposable Test Environment)")
print("Command:", " ".join(cmd))
print("=" * 70)

res = subprocess.run(cmd, cwd=str(BACKEND_DIR), env=env)
sys.exit(res.returncode)
