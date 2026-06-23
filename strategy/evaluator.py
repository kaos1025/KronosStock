"""strategy/evaluator.py — Kronos 예측의 '실력'을 숫자로 재는 walk-forward 하네스.

핵심 질문: "예측이 맞나?" 가 아니라 **"베이스라인(랜덤워크)보다 나은가?"** 와
**"확률 밴드가 약속한 만큼 실제를 덮나(calibration)?"** 를 측정한다.

왜 이게 1순위인가
------------------
일봉 종가를 N스텝 앞서 '점'으로 맞추는 건 대형주에선 거의 불가능하다(martingale).
그래서 의미 있는 평가는 point accuracy 가 아니라:
  1) 방향 적중률  vs  naive 베이스라인(추세지속/랜덤워크)  → "edge 가 있는가"
  2) MAE skill score = 1 - MAE_model / MAE_randomwalk          → ">0 이면 RW 보다 나음"
  3) 밴드 커버리지: 실제 종가가 [P10,P90] 안에 든 비율 (목표≈0.80) → "범위가 정직한가"

이 셋이 프로젝트 절대원칙 "백테스트 vs 실시간 성능 투명 공개" 를 충족하는 최소 단위다.

look-ahead 차단
---------------
원점 i 에서는 df[:i+1] (i 포함, 즉 '오늘까지 아는 정보') 만으로 예측하고,
실현값 df[i+1 : i+1+horizon] 과 비교한다. 미래 캔들은 컨텍스트에 절대 들어가지 않는다.

⚠️ CPU 비용
-----------
1 origin = n_paths 회 autoregressive 생성. CX22(2 vCPU)에서 Kronos-mini 기준
n_paths=30 × n_eval=30 ≈ 종목당 ~10분(어림)이다. 일배치 컨테이너가 아니라
**오프라인 1회성 잡**으로 돌릴 것. 빠른 스모크는 --n-eval 10 --n-paths 16.

실행:
    python -m strategy.evaluator 005930
    python -m strategy.evaluator 005930 000660 --n-eval 40 --n-paths 50 --horizon 5
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# 결과 컨테이너
# ─────────────────────────────────────────────────────────────────────
@dataclass
class EvalResult:
    """단일 종목 walk-forward 평가 산출물."""

    code: str
    n_origins: int
    horizon: int
    n_paths: int

    # 방향(horizon-end) 적중
    hit_rate: float            # 모델 median 방향 적중률
    baseline_hit_rate: float   # 추세지속(persistence) 베이스라인 적중률
    up_rate_realized: float    # 실제 상승 빈도(상승 편향 sanity)
    up_prob_mean: float        # 모델 up_probability 평균(coarse calibration)

    # 점예측 품질 (vs 랜덤워크)
    mae_model: float           # mean|median - realized| (전 스텝)
    mae_randomwalk: float      # mean|last_close - realized| (RW 예측)
    skill_score: float         # 1 - mae_model/mae_randomwalk  (>0 이면 RW 우위)

    # 밴드 정직성
    band_coverage: float       # 실제가 [P10,P90] 안에 든 비율 (목표≈ qh-ql)
    target_coverage: float     # 기대 커버리지 (qh - ql)
    mean_band_width_pct: float # 밴드 폭 / 현재가 평균(%) — 넓을수록 '확신 없음'

    per_origin: list[dict] = field(default_factory=list)

    def report(self) -> str:
        skill_tag = "✅ RW보다 나음" if self.skill_score > 0 else "❌ RW보다 못함"
        edge = self.hit_rate - self.baseline_hit_rate
        edge_tag = "✅ edge 있음" if edge > 0.02 else ("➖ 미미" if edge > -0.02 else "❌ 베이스라인 이하")
        cov_gap = self.band_coverage - self.target_coverage
        cov_tag = (
            "✅ 정직" if abs(cov_gap) <= 0.10
            else ("⚠️ 과신(밴드 좁음)" if cov_gap < 0 else "⚠️ 과보수(밴드 넓음)")
        )
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f" 📊 KronosStock 예측 평가 — {self.code}\n"
            f"    origins={self.n_origins}  horizon={self.horizon}d  n_paths={self.n_paths}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f" [방향]  모델 {self.hit_rate:.1%}  vs  베이스라인 {self.baseline_hit_rate:.1%}"
            f"   ({edge_tag}, Δ{edge:+.1%})\n"
            f"         실제 상승빈도 {self.up_rate_realized:.1%} / 모델 up_prob 평균 {self.up_prob_mean:.1%}\n"
            f" [점예측] MAE 모델 {self.mae_model:,.1f}  vs  랜덤워크 {self.mae_randomwalk:,.1f}\n"
            f"         skill score {self.skill_score:+.3f}   ({skill_tag})\n"
            f" [밴드]  커버리지 {self.band_coverage:.1%}  (목표 {self.target_coverage:.0%})  {cov_tag}\n"
            f"         평균 밴드폭 {self.mean_band_width_pct:.1f}% of price\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f" ⚠️ Not financial advice. 통계는 표본({self.n_origins})에 따라 변동.\n"
        )

    def to_dict(self, *, with_origins: bool = False) -> dict:
        """루프 영속화용 직렬화(per_origin 은 기본 제외해 파일을 작게 유지)."""
        d = {
            "code": self.code,
            "n_origins": self.n_origins,
            "horizon": self.horizon,
            "n_paths": self.n_paths,
            "hit_rate": self.hit_rate,
            "baseline_hit_rate": self.baseline_hit_rate,
            "edge": self.hit_rate - self.baseline_hit_rate,
            "up_rate_realized": self.up_rate_realized,
            "up_prob_mean": self.up_prob_mean,
            "mae_model": self.mae_model,
            "mae_randomwalk": self.mae_randomwalk,
            "skill_score": self.skill_score,
            "band_coverage": self.band_coverage,
            "target_coverage": self.target_coverage,
            "coverage_gap": self.band_coverage - self.target_coverage,
            "mean_band_width_pct": self.mean_band_width_pct,
        }
        if with_origins:
            d["per_origin"] = self.per_origin
        return d


# ─────────────────────────────────────────────────────────────────────
# walk-forward 평가
# ─────────────────────────────────────────────────────────────────────
def walk_forward_eval(
    code: str,
    *,
    df: Optional[pd.DataFrame] = None,
    n_eval: int = 30,
    horizon: int = 5,
    n_paths: int = 30,
    stride: int = 1,
    end_offset: int = 0,
    lookback: Optional[int] = None,
    quantiles: tuple[float, float, float] = (0.1, 0.5, 0.9),
) -> EvalResult:
    """code 의 과거 OHLCV 로 walk-forward 평가를 수행한다.

    df 를 주면 그걸 쓰고, 없으면 kr_data_fetcher 로 수집한다(수정주가 일관성을 위해
    가능하면 호출부에서 동일 provider 로 받은 df 를 주입할 것).

    n_eval : 평가 원점 개수(최근 구간). 클수록 통계 신뢰↑, CPU 비용↑.
    stride : 원점 간격(>1 이면 표본 줄여 빠르게).
    """
    # 무거운 의존성(redis/model)은 함수 진입 시 지연 import
    from inference.forecast_runner import get_forecaster

    if df is None:
        from inference import kr_data_fetcher
        # 평가엔 충분한 과거가 필요: lookback + horizon + n_eval*stride + 여유
        need = (lookback or 400) + horizon + n_eval * stride + end_offset + 30
        df = kr_data_fetcher.fetch_daily_ohlcv(code, days=need)

    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError(f"df 인덱스가 DatetimeIndex 가 아닙니다(code={code})")
    df = df.sort_index()
    closes = df["close"].to_numpy(dtype=float)
    n = len(df)

    ql, _, qh = quantiles
    target_coverage = qh - ql

    # 마지막 원점은 i+horizon 실현값이 있어야 하므로 n-1-horizon 까지.
    # end_offset>0 이면 최근 구간을 비워 별도 confirm(홀드아웃) 윈도우를 만든다.
    last_origin = n - 1 - horizon - end_offset
    first_origin = last_origin - (n_eval - 1) * stride
    min_needed = (lookback or 100) + 5
    if first_origin < min_needed:
        first_origin = min_needed
    origins = list(range(first_origin, last_origin + 1, stride))
    if not origins:
        raise ValueError(
            f"데이터 부족: n={n}, horizon={horizon}, n_eval={n_eval}. "
            f"더 긴 기간을 수집하거나 n_eval/horizon 을 줄이세요."
        )

    forecaster = get_forecaster()

    hits = baseline_hits = ups_realized = 0
    abs_err_model: list[float] = []
    abs_err_rw: list[float] = []
    in_band = 0
    band_steps = 0
    band_width_pcts: list[float] = []
    up_probs: list[float] = []
    per_origin: list[dict] = []

    logger.info("walk-forward 시작: %s origins=%d (idx %d~%d)", code, len(origins), origins[0], origins[-1])

    for n_done, i in enumerate(origins, 1):
        ctx = df.iloc[: i + 1]                     # 'i 시점까지 아는 정보'만
        last_close = closes[i]
        realized = closes[i + 1 : i + 1 + horizon]  # 실현 종가 horizon개
        if len(realized) < horizon:
            continue

        res = forecaster.predict_probabilistic(
            ctx, code=code, horizon=horizon, n_paths=n_paths,
            lookback=lookback, freq="B", quantiles=quantiles,
        )
        median = np.asarray(res.median_close, dtype=float)
        lower = np.asarray(res.lower_close, dtype=float)
        upper = np.asarray(res.upper_close, dtype=float)

        # 방향(horizon-end)
        model_up = median[-1] > last_close
        real_up = realized[-1] > last_close
        if model_up == real_up:
            hits += 1
        if real_up:
            ups_realized += 1
        up_probs.append(float(res.up_probability))

        # 베이스라인: 추세지속(직전 수익률 부호가 이어진다)
        prev_ret = closes[i] - closes[i - 1] if i >= 1 else 0.0
        base_up = prev_ret >= 0.0
        if base_up == real_up:
            baseline_hits += 1

        # 점예측 오차 (전 스텝): 모델 vs 랜덤워크(last_close 고정)
        abs_err_model.extend(np.abs(median - realized).tolist())
        abs_err_rw.extend(np.abs(last_close - realized).tolist())

        # 밴드 커버리지(전 스텝) + 폭
        in_band += int(np.sum((realized >= lower) & (realized <= upper)))
        band_steps += horizon
        band_width_pcts.extend(((upper - lower) / last_close * 100.0).tolist())

        per_origin.append({
            "date": df.index[i].strftime("%Y-%m-%d"),
            "last_close": float(last_close),
            "median_end": float(median[-1]),
            "realized_end": float(realized[-1]),
            "model_up": bool(model_up),
            "real_up": bool(real_up),
            "up_prob": float(res.up_probability),
        })

        if n_done % 5 == 0 or n_done == len(origins):
            logger.info("  %d/%d origins 완료 (현재 적중 %d)", n_done, len(origins), hits)

    m = len(per_origin)
    if m == 0:
        raise ValueError("유효한 평가 원점이 0개입니다. 기간/horizon 을 확인하세요.")

    mae_model = float(np.mean(abs_err_model))
    mae_rw = float(np.mean(abs_err_rw))
    skill = 1.0 - mae_model / mae_rw if mae_rw > 0 else 0.0

    return EvalResult(
        code=code,
        n_origins=m,
        horizon=horizon,
        n_paths=n_paths,
        hit_rate=hits / m,
        baseline_hit_rate=baseline_hits / m,
        up_rate_realized=ups_realized / m,
        up_prob_mean=float(np.mean(up_probs)),
        mae_model=mae_model,
        mae_randomwalk=mae_rw,
        skill_score=skill,
        band_coverage=in_band / band_steps if band_steps else 0.0,
        target_coverage=target_coverage,
        mean_band_width_pct=float(np.mean(band_width_pcts)) if band_width_pcts else 0.0,
        per_origin=per_origin,
    )


def evaluate_watchlist(
    codes: Optional[Sequence[str]] = None, **kwargs
) -> dict[str, EvalResult]:
    """워치리스트(또는 지정 종목) 전체 평가. 종목 실패는 로깅 후 계속."""
    from common.config import settings

    targets = list(codes) if codes else list(settings.watchlist)
    out: dict[str, EvalResult] = {}
    for code in targets:
        try:
            out[code] = walk_forward_eval(code, **kwargs)
            print(out[code].report())
        except Exception as exc:  # noqa: BLE001
            logger.error("평가 실패: %s: %s", code, exc)
    return out


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="KronosStock 예측 walk-forward 평가")
    p.add_argument("codes", nargs="*", help="종목코드(없으면 watchlist)")
    p.add_argument("--n-eval", type=int, default=30, help="평가 원점 개수(기본 30)")
    p.add_argument("--horizon", type=int, default=5, help="예측 스텝(기본 5일)")
    p.add_argument("--n-paths", type=int, default=30, help="Monte Carlo 경로(기본 30)")
    p.add_argument("--stride", type=int, default=1, help="원점 간격(기본 1)")
    p.add_argument("--end-offset", type=int, default=0, help="최근 N세션을 비워 confirm 윈도우 분리")
    p.add_argument("--lookback", type=int, default=None, help="컨텍스트 봉 수(기본 config)")
    return p


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    args = _build_arg_parser().parse_args()
    results = evaluate_watchlist(
        codes=args.codes or None,
        n_eval=args.n_eval,
        horizon=args.horizon,
        n_paths=args.n_paths,
        stride=args.stride,
        end_offset=args.end_offset,
        lookback=args.lookback,
    )
    if results:
        avg_skill = float(np.mean([r.skill_score for r in results.values()]))
        avg_edge = float(np.mean([r.hit_rate - r.baseline_hit_rate for r in results.values()]))
        avg_cov = float(np.mean([r.band_coverage for r in results.values()]))
        print(f"\n📈 종합({len(results)}종목): skill 평균 {avg_skill:+.3f} | "
              f"방향 edge 평균 {avg_edge:+.1%} | 커버리지 평균 {avg_cov:.1%}")
