"""Run every pytest module in a fresh process, with bounded time and durable results.

Qt windows/native inference resources from unrelated modules must not accumulate
in one long-lived process. No test is retried or silently ignored on failure.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
# App-driving scripts call os._exit at import; match tests/conftest.py collect_ignore.
APP_SCRIPTS = {"test_project_app.py", "test_project_large.py",
               "test_project_panel.py", "test_start_window.py"}
SCRIPT_CHECKS = {"test_project_format.py", "test_project_storage.py"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--output", default="artifacts/test-results")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = [Path(path) for path in args.paths] if args.paths else sorted((ROOT / "tests").glob("test_*.py"))
    results = []
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen",
               MEENIFX_NO_RECENT="1")
    for path in paths:
        if path.name in APP_SCRIPTS:
            continue
        log, report = output / (path.stem + ".log"), output / (path.stem + ".xml")
        # A crashed rerun must never inherit counts from an older successful report.
        report.unlink(missing_ok=True)
        command = [sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:cacheprovider",
                   "--junitxml=" + str(report)]
        if path.name in SCRIPT_CHECKS:
            # These files use top-level assertions, not pytest functions.
            command = [sys.executable, str(path)]
        started = time.monotonic()
        with log.open("w", encoding="utf-8") as stream:
            try:
                run = subprocess.run(command, cwd=ROOT, env=env, stdout=stream,
                                     stderr=subprocess.STDOUT, timeout=args.timeout)
                code = run.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
        item = {"file": path.name, "exit_code": code,
                "kind": "script" if path.name in SCRIPT_CHECKS else "pytest",
                "seconds": round(time.monotonic() - started, 2)}
        if report.exists():
            suite = ET.parse(report).getroot().find("testsuite")
            if suite is not None:
                item.update({key: int(suite.get(key, 0))
                             for key in ("tests", "failures", "errors", "skipped")})
        results.append(item)
        (output / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(item), flush=True)
    return int(any(item["exit_code"] != 0 for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
