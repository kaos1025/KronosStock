"""
inference/predictor.py — Kronos 파운데이션 모델 래퍼 (도메인 비종속).

shiyu-coder/Kronos README(master) 기준으로 시그니처 검증:
  - 로딩:   from model import Kronos, KronosTokenizer, KronosPredictor
            tokenizer = KronosTokenizer.from_pretrained(<tokenizer>)
            model     = Kronos.from_pretrained(<model>)
  - 생성자: KronosPredictor(model, tokenizer, device="cpu", max_context=512)
  - 예측:   predictor.predict(df, x_timestamp, y_timestamp, pred_len, T, top_p, sample_count)
            -> DataFrame[open, high, low, close, (volume, amount)] (y_timestamp 인덱스)

KronosStock(KR 주식)과 KronosKit(크립토)에서 공유하기 위해 도메인 비종속으로 작성.
미래 타임스탬프 생성(freq, 휴장일)만 호출부에서 주입한다.

⚠️ 사전 준비(vendoring): Kronos의 `model/` 패키지가 repo 루트에 있어야 한다.
   inference/vendor_kronos.sh 실행으로 가져온다.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# --- Vendored Kronos 패키지 부트스트랩 ----------------------------------------
# Kronos `model/` 패키지를 repo 루트에서 import 가능하도록 sys.path에 추가.
# (CWD와 무관하게 `from model import ...` 가 해석되도록)
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# --- 모델 ↔ 토크나이저 매핑 (★ 1번 함정 방지) --------------------------------
# Kronos-mini 는 반드시 Kronos-Tokenizer-2k (context 2048).
# Kronos-small / base 는 Kronos-Tokenizer-base (context 512).
_MODEL_TOKENIZER_MAP: dict[str, tuple[str, int]] = {
    "NeoQuasar/Kronos-mini":  ("NeoQuasar/Kronos-Tokenizer-2k",   2048),
    "NeoQuasar/Kronos-small": ("NeoQuasar/Kronos-Tokenizer-base", 512),
    "NeoQuasar/Kronos-base":  ("NeoQuasar/Kronos-Tokenizer-base", 512),
}

REQUIRED_COLS = ["open", "high", "low", "close"]
OPTIONAL_COLS = ["volume", "amount"]


@dataclass
class ForecastResult:
    """확률적 예측 결과 (Monte Carlo fan)."""
    code: str
    last_close: float
    horizon: int
    timestamps: pd.DatetimeIndex      # 미래 구간 타임스탬프 (length == horizon)
    median_close: np.ndarray          # 스텝별 중앙값 (point forecast)
    lower_close: np.ndarray           # 하단 분위수 밴드
    upper_close: np.ndarray           # 상단 분위수 밴드
    up_probability: float             # 마지막 스텝이 현재가보다 높게 끝난 경로 비율
    n_paths: int
    quantiles: tuple[float, float, float]
    paths_close: Optional[np.ndarray] = None  # (n_paths, horizon), keep_paths=True일 때만

    def summary(self) -> str:
        lo, hi = self.lower_close[-1], self.upper_close[-1]
        mid = self.median_close[-1]
        chg = (mid / self.last_close - 1.0) * 100.0
        ql, _, qh = self.quantiles
        return (
            f"[{self.code}] last={self.last_close:,.2f} "
            f"-> +{self.horizon} step median={mid:,.2f} ({chg:+.2f}%) "
            f"| {int(ql*100)}~{int(qh*100)}% band: {lo:,.2f} ~ {hi:,.2f} "
            f"| up_prob={self.up_probability*100:.0f}% (n={self.n_paths})"
        )


class KronosForecaster:
    """
    Kronos 모델 래퍼.

    사용 예:
        fc = KronosForecaster(model_name="NeoQuasar/Kronos-mini", device="cpu")
        fc.load()
        res = fc.predict_probabilistic(df, code="005930", horizon=5, n_paths=20)
        print(res.summary())
    """

    def __init__(
        self,
        model_name: str = "NeoQuasar/Kronos-mini",
        device: str = "cpu",
        tokenizer_name: Optional[str] = None,
        max_context: Optional[int] = None,
    ) -> None:
        if model_name in _MODEL_TOKENIZER_MAP:
            default_tok, default_ctx = _MODEL_TOKENIZER_MAP[model_name]
        else:
            # 매핑에 없는 커스텀/파인튜닝 모델: 토크나이저를 명시해야 함
            default_tok, default_ctx = (None, 512)
            if tokenizer_name is None:
                raise ValueError(
                    f"Unknown model '{model_name}'. "
                    f"tokenizer_name 을 명시하세요. 알려진 모델: {list(_MODEL_TOKENIZER_MAP)}"
                )

        self.model_name = model_name
        self.tokenizer_name = tokenizer_name or default_tok
        self.device = device
        self.max_context = max_context or default_ctx

        self._model = None
        self._tokenizer = None
        self._predictor = None

        logger.info(
            "KronosForecaster init: model=%s tokenizer=%s device=%s max_context=%d",
            self.model_name, self.tokenizer_name, self.device, self.max_context,
        )

    # --- 로딩 ---------------------------------------------------------------
    def load(self) -> "KronosForecaster":
        """모델/토크나이저/Predictor 로드 (최초 1회, 가중치는 HF Hub에서 캐싱)."""
        if self._predictor is not None:
            return self
        try:
            from model import Kronos, KronosTokenizer, KronosPredictor  # type: ignore
        except ImportError as e:
            raise ImportError(
                "Kronos `model/` 패키지를 찾을 수 없습니다. "
                "inference/vendor_kronos.sh 를 실행해 repo 루트에 vendoring 하세요."
            ) from e

        logger.info("Loading tokenizer: %s", self.tokenizer_name)
        self._tokenizer = KronosTokenizer.from_pretrained(self.tokenizer_name)
        logger.info("Loading model: %s", self.model_name)
        self._model = Kronos.from_pretrained(self.model_name)
        self._predictor = KronosPredictor(
            self._model, self._tokenizer,
            device=self.device, max_context=self.max_context,
        )
        logger.info("KronosPredictor ready.")
        return self

    def _ensure_loaded(self) -> None:
        if self._predictor is None:
            self.load()

    # --- 입력 정규화 ---------------------------------------------------------
    @staticmethod
    def _resolve_x(
        df: pd.DataFrame,
        x_timestamp: Optional[pd.Series],
    ) -> tuple[pd.DataFrame, pd.Series]:
        """OHLCV df와 타임스탬프 Series를 검증·정렬해서 반환."""
        cols = [c for c in (REQUIRED_COLS + OPTIONAL_COLS) if c in df.columns]
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(f"df에 필수 컬럼 누락: {missing} (필수: {REQUIRED_COLS})")

        x_df = df[cols].copy().reset_index(drop=True)

        if x_timestamp is not None:
            ts = pd.to_datetime(pd.Series(x_timestamp).reset_index(drop=True))
        elif isinstance(df.index, pd.DatetimeIndex):
            ts = pd.Series(pd.to_datetime(df.index)).reset_index(drop=True)
        else:
            for cand in ("timestamps", "timestamp", "date", "datetime"):
                if cand in df.columns:
                    ts = pd.to_datetime(df[cand]).reset_index(drop=True)
                    break
            else:
                raise ValueError(
                    "x_timestamp를 줄 수 없으면 df가 DatetimeIndex이거나 "
                    "['timestamps','date',...] 컬럼이 있어야 합니다."
                )
        if len(ts) != len(x_df):
            raise ValueError(f"타임스탬프 길이({len(ts)}) != df 길이({len(x_df)})")
        return x_df, ts

    @staticmethod
    def _future_index(
        last_ts: pd.Timestamp,
        horizon: int,
        freq: str = "B",
        holidays: Optional[Sequence] = None,
    ) -> pd.DatetimeIndex:
        """
        미래 타임스탬프 생성.
          freq="B"  : 영업일(월~금). KR 공휴일은 holidays로 제거(선택).
          freq="h"  : 1시간봉(크립토 등).
        """
        last_ts = pd.Timestamp(last_ts)
        if freq == "B":
            hol = pd.to_datetime(list(holidays)) if holidays else None
            # 공휴일 제거 후 horizon개 확보될 때까지 넉넉히 뽑아서 자른다
            span = pd.bdate_range(
                start=last_ts + pd.Timedelta(days=1),
                periods=horizon + (len(hol) if hol is not None else 0) + 10,
                freq="C" if hol is not None else "B",
                holidays=hol if hol is not None else None,
            )
            return span[:horizon]
        # 일반 freq (시간/분 등)
        return pd.date_range(start=last_ts, periods=horizon + 1, freq=freq)[1:]

    # --- 단일 경로 예측 ------------------------------------------------------
    def predict_from_df(
        self,
        df: pd.DataFrame,
        *,
        horizon: int = 5,
        x_timestamp: Optional[pd.Series] = None,
        y_timestamp: Optional[pd.Series] = None,
        lookback: Optional[int] = None,
        freq: str = "B",
        holidays: Optional[Sequence] = None,
        T: float = 1.0,
        top_p: float = 0.9,
        sample_count: int = 1,
    ) -> pd.DataFrame:
        """1회 예측(평균 경로). 원시 Kronos 출력 DataFrame을 그대로 반환."""
        self._ensure_loaded()
        x_df, ts = self._resolve_x(df, x_timestamp)

        lb = lookback or min(len(x_df), self.max_context)
        x_df = x_df.tail(lb).reset_index(drop=True)
        ts = ts.tail(lb).reset_index(drop=True)

        if y_timestamp is None:
            y_idx = self._future_index(ts.iloc[-1], horizon, freq=freq, holidays=holidays)
            y_ts = pd.Series(y_idx)
        else:
            y_ts = pd.to_datetime(pd.Series(y_timestamp)).reset_index(drop=True)
            horizon = len(y_ts)

        pred_df = self._predictor.predict(
            df=x_df,
            x_timestamp=ts,
            y_timestamp=y_ts,
            pred_len=horizon,
            T=T,
            top_p=top_p,
            sample_count=sample_count,
        )
        return pred_df

    # --- 확률적 예측 (Monte Carlo fan) --------------------------------------
    def predict_probabilistic(
        self,
        df: pd.DataFrame,
        *,
        code: str = "",
        horizon: int = 5,
        n_paths: int = 20,
        x_timestamp: Optional[pd.Series] = None,
        lookback: Optional[int] = None,
        freq: str = "B",
        holidays: Optional[Sequence] = None,
        T: float = 1.0,
        top_p: float = 0.9,
        quantiles: tuple[float, float, float] = (0.1, 0.5, 0.9),
        keep_paths: bool = False,
    ) -> ForecastResult:
        """
        n_paths개의 독립 표본 경로를 생성해 분위수 밴드/상승확률을 계산.
        CPU에서는 (모델 크기 × n_paths × horizon)에 비례해 느려지므로
        개인용 일배치(소수 종목, n_paths≈20)에 적합.
        """
        self._ensure_loaded()
        x_df, ts = self._resolve_x(df, x_timestamp)
        lb = lookback or min(len(x_df), self.max_context)
        x_df = x_df.tail(lb).reset_index(drop=True)
        ts = ts.tail(lb).reset_index(drop=True)

        last_close = float(x_df["close"].iloc[-1])
        y_idx = self._future_index(ts.iloc[-1], horizon, freq=freq, holidays=holidays)
        y_ts = pd.Series(y_idx)

        closes = np.empty((n_paths, horizon), dtype=float)
        for i in range(n_paths):
            pred_df = self._predictor.predict(
                df=x_df,
                x_timestamp=ts,
                y_timestamp=y_ts,
                pred_len=horizon,
                T=T,
                top_p=top_p,
                sample_count=1,   # 경로 1개씩 → 분포 확보
            )
            closes[i, :] = pred_df["close"].to_numpy()[:horizon]

        ql, qm, qh = quantiles
        lower = np.quantile(closes, ql, axis=0)
        median = np.quantile(closes, qm, axis=0)
        upper = np.quantile(closes, qh, axis=0)
        up_prob = float(np.mean(closes[:, -1] > last_close))

        return ForecastResult(
            code=code,
            last_close=last_close,
            horizon=horizon,
            timestamps=pd.DatetimeIndex(y_idx),
            median_close=median,
            lower_close=lower,
            upper_close=upper,
            up_probability=up_prob,
            n_paths=n_paths,
            quantiles=quantiles,
            paths_close=closes if keep_paths else None,
        )


# --- 합성 데이터 스모크 테스트 (KIS 키/Redis 불필요) --------------------------
# 주의: 최초 실행 시 HuggingFace Hub에서 가중치를 내려받는다(인터넷 필요).
#       Docker(3.11) 안에서 vendoring + 의존성 설치 후 실행할 것.
#         python -m inference.predictor
def _make_synthetic_daily(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    rets = rng.normal(0.0003, 0.018, size=n)
    close = 70000 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    df = _make_synthetic_daily()
    fc = KronosForecaster(model_name="NeoQuasar/Kronos-mini", device="cpu")
    res = fc.predict_probabilistic(df, code="TEST", horizon=5, n_paths=8, keep_paths=True)
    print(res.summary())
    print("future timestamps:", list(res.timestamps.strftime("%Y-%m-%d")))
    print("median path:", np.round(res.median_close, 2).tolist())
