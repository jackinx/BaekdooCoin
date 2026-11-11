# -*- coding: utf-8 -*-
"""머지 게이트 — 테스트가 통과한 모듈만 커밋/머지되게 강제한다.

  python tools/gate.py               # 전체 테스트 + 모듈별 커버리지 임계
  python tools/gate.py --fast        # 테스트만
  python tools/gate.py --min 80      # 커버리지 임계 변경(기본 70)

pre-commit 훅(.git/hooks/pre-commit)이 이 스크립트를 호출한다.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

# 실전 경로에 있는 모듈은 커버리지 하한을 더 높게 잡는다 (돈이 직접 오가는 코드)
CRITICAL = {
    "upbit_client.py": 70,
    "engine_mtf.py": 75,
    "strategies/risk_mtf.py": 80,
    "strategies/turtle_mtf.py": 75,
    "strategies/regime_detector.py": 70,
}


def run_pytest(fast: bool) -> tuple[int, dict]:
    cov_file = ROOT / ".coverage.json"
    cmd = [PY, "-m", "pytest", "tests", "-q", "-p", "no:warnings"]
    if not fast:
        cmd += ["--cov=.", "--cov-report=json:" + str(cov_file), "--cov-report=term-missing:skip-covered"]
    proc = subprocess.run(cmd, cwd=ROOT)
    data = {}
    if cov_file.exists():
        try:
            data = json.loads(cov_file.read_text(encoding="utf-8"))
        finally:
            cov_file.unlink(missing_ok=True)
    return proc.returncode, data


def coverage_gate(data: dict, default_min: int) -> list[str]:
    failures: list[str] = []
    files = data.get("files", {})
    for name, info in files.items():
        rel = name.replace("\\", "/")
        percent = info["summary"]["percent_covered"]
        threshold = CRITICAL.get(rel, default_min)
        if rel.startswith("tests/") or rel in ("pytest.ini",):
            continue
        if percent < threshold:
            failures.append(f"{rel}: {percent:.1f}% < {threshold}%")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--min", type=int, default=60)
    args = ap.parse_args()

    code, data = run_pytest(args.fast)
    if code != 0:
        print("\n❌ 게이트 실패: 테스트 실패 — 커밋/머지 중단")
        return code

    if args.fast or not data:
        print("\n✅ 게이트 통과 (테스트만)")
        return 0

    failures = coverage_gate(data, args.min)
    total = data.get("totals", {}).get("percent_covered", 0.0)
    print(f"\n전체 커버리지: {total:.1f}%")
    if failures:
        print("❌ 커버리지 미달 모듈:")
        for f in failures:
            print("   -", f)
        return 4
    print("✅ 게이트 통과: 테스트 + 커버리지")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
