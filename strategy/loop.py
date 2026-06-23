"""strategy/loop.py — Kronos 예측 개선의 '닫힌 루프'.

evaluator.py 가 '측정'이라면 이 파일은 '루프'다:
    baseline 고정  →  변경 1개  →  try(재측정)  →  ACCEPT/REJECT 판정  →  promote
매 반복을 직전 baseline 과 비교해, **노이즈를 넘는 개선분만** 채택한다.

왜 임계값을 두는가
------------------
n_eval=30~40 origins 에서 방향 적중률은 표본오차가 크다(±수 %p 는 그냥 운).
임계값 없이 "올랐다"를 채택하면 노이즈에 과적합한다. 그래서:
  - ACCEPT 하려면 북극성 지표 중 하나가 **MIN_*_GAIN 이상** 개선
  - 동시에 어떤 지표도 **REGRESS_* 이상 악화되지 않을 것**
  - (선택) --confirm-offset 로 '다른 과거 윈도우'에서도 무너지지 않을 것 → 과적합 방지

북극성 지표(낮을수록/높을수록 좋은지 명시)
  edge          = 모델 방향적중 − 베이스라인  (↑ 좋음, 음수면 동전던지기 이하)
  skill_score   = 1 − MAE_model/MAE_rw       (↑ 좋음, ≤0 이면 랜덤워크 이하)
  coverage_gap  = |밴드 커버리지 − 목표|       (↓ 좋음, 0 이면 밴드가 정직)

영속화: eval_runs/<name>.json  (config 지문 + 종목별/평균 지표 + git sha)
        → .gitignore 에 eval_runs/ 추가 권장.

사용:
    python -m strategy.loop baseline 005930 000660 --n-eval 40 --n-paths 50
    # ... 코드/하이퍼파라미터 1개 변경 ...
    python -m strategy.loop try      005930 000660 --n-eval 40 --n-paths 50 --confirm-offset 20
    python -m strategy.loop promote          # candidate 가 ACCEPT 면 baseline 으로 승격
    python -m strategy.loop show
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RUNS_DIR = Path("eval_runs")
BASELINE = RUNS_DIR / "baseline.json"
CANDIDATE = RUNS_DIR / "candidate.json"

# ── 노이즈를 넘어야 채택. 표본이 작을수록 보수적으로(값 키우기). ──
MIN_EDGE_GAIN = 0.03      # 방향 edge +3%p 이상이어야 의미
MIN_SKILL_GAIN = 0.02     # skill +0.02 이상
MIN_COVERAGE_GAIN = 0.03  # coverage_gap 0.03 이상 축소(목표에 근접)
REGRESS_EDGE = 0.03       # edge 이만큼 악화되면 거부
REGRESS_SKILL = 0.02
REGRESS_COVERAGE = 0.05   # coverage_gap 이만큼 벌어지면 거부

# config 지문에서 '같은 잣대'를 보장해야 하는 키(다르면 비교 부당)
SHAPE_KEYS = ("codes", "n_eval", "horizon", "stride", "end_offset", "lookback")


# ─────────────────────────────────────────────────────────────────────
def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


@dataclass
class LoopRun:
    """한 번의 평가 실행을 요약(종목 평균 + 종목별 + config 지문)."""

    config: dict
    git_sha: str
    created_at: str
    avg: dict              # {edge, skill_score, coverage_gap, hit_rate, band_coverage}
    per_code: dict         # {code: EvalResult.to_dict()}
    confirm_avg: Optional[dict] = None  # --confirm-offset 시 다른 윈도우 평균

    def to_json(self) -> dict:
        return {
            "config": self.config,
            "git_sha": self.git_sha,
            "created_at": self.created_at,
            "avg": self.avg,
            "confirm_avg": self.confirm_avg,
            "per_code": self.per_code,
        }


def _aggregate(results: dict) -> dict:
    """{code: EvalResult} → 평균 지표 dict."""
    import numpy as np
    rows = [r.to_dict() for r in results.values()]
    keys = ["edge", "skill_score", "coverage_gap", "hit_rate", "baseline_hit_rate", "band_coverage"]
    return {k: float(np.mean([row[k] for row in rows])) for k in keys}


def run_eval(codes, *, n_eval, horizon, n_paths, stride, end_offset, lookback,
             confirm_offset: int = 0) -> LoopRun:
    """현재 코드/설정으로 평가를 돌려 LoopRun 으로 요약한다."""
    from strategy.evaluator import evaluate_watchlist

    common = dict(n_eval=n_eval, horizon=horizon, n_paths=n_paths,
                  stride=stride, lookback=lookback)
    logger.info("평가(recent window) 시작 …")
    results = evaluate_watchlist(codes=codes, end_offset=end_offset, **common)
    if not results:
        raise RuntimeError("평가 결과가 비었습니다(모든 종목 실패).")

    confirm_avg = None
    if confirm_offset > 0:
        logger.info("확인(confirm window, end_offset=%d) 시작 …", confirm_offset)
        confirm = evaluate_watchlist(codes=codes, end_offset=end_offset + confirm_offset, **common)
        if confirm:
            confirm_avg = _aggregate(confirm)

    config = {
        "codes": list(codes) if codes else "watchlist",
        "n_eval": n_eval, "horizon": horizon, "n_paths": n_paths,
        "stride": stride, "end_offset": end_offset, "lookback": lookback,
        "confirm_offset": confirm_offset,
    }
    return LoopRun(
        config=config,
        git_sha=_git_sha(),
        created_at=datetime.now().astimezone().isoformat(),
        avg=_aggregate(results),
        per_code={c: r.to_dict() for c, r in results.items()},
        confirm_avg=confirm_avg,
    )


def _save(run: LoopRun, path: Path) -> None:
    RUNS_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(run.to_json(), ensure_ascii=False, indent=2))
    logger.info("저장: %s", path)


def _load(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text())


# ─────────────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────────────
def verdict(baseline: dict, candidate: dict) -> tuple[bool, list[str]]:
    """candidate 가 baseline 대비 ACCEPT 인지 판정. (accept, 사유줄들) 반환."""
    notes: list[str] = []

    # 1) 같은 잣대인지(config 지문) 확인
    bshape = {k: baseline["config"].get(k) for k in SHAPE_KEYS}
    cshape = {k: candidate["config"].get(k) for k in SHAPE_KEYS}
    if bshape != cshape:
        diff = {k: (bshape[k], cshape[k]) for k in SHAPE_KEYS if bshape[k] != cshape[k]}
        notes.append(f"⚠️ config 지문 불일치 → 비교 부당: {diff}")
        notes.append("   (같은 n_eval/horizon/stride/codes 로 baseline 을 다시 잡으세요)")
        return False, notes

    b, c = baseline["avg"], candidate["avg"]
    d_edge = c["edge"] - b["edge"]
    d_skill = c["skill_score"] - b["skill_score"]
    # 커버리지는 '목표와의 거리(절댓값)'로 본다. 71%→78% 처럼 목표(80%)에 가까워지면 개선.
    b_absgap = abs(b["coverage_gap"])
    c_absgap = abs(c["coverage_gap"])
    d_absgap = c_absgap - b_absgap            # 음수 = 목표에 근접(개선)

    notes.append(f"edge   {b['edge']:+.1%} → {c['edge']:+.1%}  (Δ{d_edge:+.1%})")
    notes.append(f"skill  {b['skill_score']:+.3f} → {c['skill_score']:+.3f}  (Δ{d_skill:+.3f})")
    notes.append(f"cov_gap |{b['coverage_gap']:+.1%}| → |{c['coverage_gap']:+.1%}|  (Δ거리 {d_absgap:+.1%}, 음수=목표근접)")

    improved = (d_edge >= MIN_EDGE_GAIN) or (d_skill >= MIN_SKILL_GAIN) or (d_absgap <= -MIN_COVERAGE_GAIN)
    regressed = (d_edge <= -REGRESS_EDGE) or (d_skill <= -REGRESS_SKILL) or (d_absgap >= REGRESS_COVERAGE)

    if regressed:
        notes.append("→ ❌ REJECT: 어떤 지표가 허용폭 이상 악화됨")
        return False, notes
    if not improved:
        notes.append("→ ➖ REJECT: 개선폭이 노이즈 임계값 미만(운일 수 있음)")
        return False, notes

    # 2) 홀드아웃 확인: candidate 의 다른 윈도우가 baseline 보다 크게 무너지면 과적합 의심
    if candidate.get("confirm_avg"):
        cc = candidate["confirm_avg"]
        if (cc["edge"] <= b["edge"] - REGRESS_EDGE) and (cc["skill_score"] <= b["skill_score"] - REGRESS_SKILL):
            notes.append(f"→ ⚠️ REJECT: confirm 윈도우에서 무너짐(edge {cc['edge']:+.1%}, skill {cc['skill_score']:+.3f}) → 과적합 의심")
            return False, notes
        notes.append(f"   confirm 윈도우 OK (edge {cc['edge']:+.1%}, skill {cc['skill_score']:+.3f})")

    notes.append("→ ✅ ACCEPT: 노이즈를 넘는 개선 + 회귀 없음")
    return True, notes


# ─────────────────────────────────────────────────────────────────────
# 명령
# ─────────────────────────────────────────────────────────────────────
def cmd_baseline(args) -> None:
    run = run_eval(args.codes or None, n_eval=args.n_eval, horizon=args.horizon,
                   n_paths=args.n_paths, stride=args.stride, end_offset=args.end_offset,
                   lookback=args.lookback)
    _save(run, BASELINE)
    print(f"\n📌 baseline 고정 (git {run.git_sha}) — edge {run.avg['edge']:+.1%} | "
          f"skill {run.avg['skill_score']:+.3f} | cov_gap {run.avg['coverage_gap']:+.1%}")


def cmd_try(args) -> None:
    base = _load(BASELINE)
    if base is None:
        raise SystemExit("baseline 이 없습니다. 먼저 `loop baseline ...` 을 실행하세요.")
    run = run_eval(args.codes or None, n_eval=args.n_eval, horizon=args.horizon,
                   n_paths=args.n_paths, stride=args.stride, end_offset=args.end_offset,
                   lookback=args.lookback, confirm_offset=args.confirm_offset)
    _save(run, CANDIDATE)
    accept, notes = verdict(base, run.to_json())
    print("\n" + "━" * 44)
    print(f" 🔁 LOOP 판정  (baseline git {base['git_sha']} → candidate git {run.git_sha})")
    print("━" * 44)
    for n in notes:
        print(" " + n)
    print("━" * 44)
    print(" 채택이면 `python -m strategy.loop promote` 로 baseline 갱신.\n"
          if accept else " 거부 → 변경을 되돌리거나 다른 가설을 시도하세요.\n")


def cmd_promote(args) -> None:
    cand = _load(CANDIDATE)
    if cand is None:
        raise SystemExit("candidate 가 없습니다. 먼저 `loop try ...` 를 실행하세요.")
    base = _load(BASELINE)
    if base is not None:
        accept, _ = verdict(base, cand)
        if not accept and not args.force:
            raise SystemExit("candidate 가 ACCEPT 상태가 아닙니다. 정말 승격하려면 --force.")
    _save(LoopRun(**{k: cand[k] for k in ("config", "git_sha", "created_at", "avg", "confirm_avg", "per_code")}), BASELINE)
    print(f"📌 promote 완료 → baseline = candidate (git {cand['git_sha']})")


def cmd_show(args) -> None:
    for label, path in (("BASELINE", BASELINE), ("CANDIDATE", CANDIDATE)):
        d = _load(path)
        if d is None:
            print(f"{label}: (없음)")
            continue
        a = d["avg"]
        print(f"{label}: git {d['git_sha']} @ {d['created_at'][:19]} | "
              f"edge {a['edge']:+.1%} | skill {a['skill_score']:+.3f} | "
              f"cov_gap {a['coverage_gap']:+.1%} | config={d['config']}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="KronosStock 예측 개선 루프")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("codes", nargs="*", help="종목코드(없으면 watchlist)")
        sp.add_argument("--n-eval", type=int, default=30)
        sp.add_argument("--horizon", type=int, default=5)
        sp.add_argument("--n-paths", type=int, default=30)
        sp.add_argument("--stride", type=int, default=1)
        sp.add_argument("--end-offset", type=int, default=0)
        sp.add_argument("--lookback", type=int, default=None)

    sb = sub.add_parser("baseline", help="현재 상태를 baseline 으로 고정")
    add_common(sb)
    sb.set_defaults(func=cmd_baseline)

    st = sub.add_parser("try", help="candidate 평가 + baseline 대비 판정")
    add_common(st)
    st.add_argument("--confirm-offset", type=int, default=0, help="홀드아웃 확인 윈도우 분리(권장 ≥20)")
    st.set_defaults(func=cmd_try)

    sp_ = sub.add_parser("promote", help="candidate 를 baseline 으로 승격")
    sp_.add_argument("--force", action="store_true")
    sp_.set_defaults(func=cmd_promote)

    ss = sub.add_parser("show", help="현재 baseline/candidate 지표 출력")
    ss.set_defaults(func=cmd_show)
    return p


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    args = _build_parser().parse_args()
    args.func(args)
