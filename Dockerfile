# KronosStock — Python 3.11 (CLAUDE.md spec).  CPU inference (GPU 없음).
FROM python:3.11-slim

# 한국 장시간 기준 스케줄링을 위해 타임존 고정
ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

# tzdata: KST 스케줄링 / git: Kronos model/ vendoring(sparse-checkout)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata git \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치(레이어 캐시 활용). requirements.txt 의 --extra-index-url 이
# torch CPU 휠을 가져온다. pip 을 고정해 resolver 동작을 재현 가능하게 하고,
# 빌드 시점에 핵심 의존성을 import 해 ABI/누락 문제를 조기에 잡는다.
COPY requirements.txt .
RUN pip install --upgrade "pip==24.3.1" \
    && pip install -r requirements.txt \
    && python -c "import torch,numpy,pandas,matplotlib,pykrx,FinanceDataReader,einops,safetensors,huggingface_hub,exchange_calendars,fastapi,uvicorn,pydantic,pydantic_settings,redis,apscheduler,telegram,httpx; torch.from_numpy(numpy.zeros(1)); print('deps OK + np<->torch interop -> torch', torch.__version__, '| pandas', pandas.__version__, '| numpy', numpy.__version__)"

# 애플리케이션 코드 (.dockerignore 가 .env / .git / 모델 가중치 등을 제외)
COPY . .

# Kronos `model/` 패키지 vendoring(git sparse-checkout, 빌드 시 1회) + import 검증.
# 가중치는 런타임 첫 추론 때 HF Hub 에서 HF_HOME(=/app/.cache/huggingface)로 다운로드.
RUN bash inference/vendor_kronos.sh \
    && python -c "from model import Kronos, KronosTokenizer, KronosPredictor; print('vendored model import OK')"

# 토큰/HF 캐시 / 로그 디렉터리(비루트 사용자 소유) — vendored model/ 도 chown 대상
RUN mkdir -p /app/.cache/huggingface /app/logs \
    && useradd -m -u 1000 kronos \
    && chown -R kronos:kronos /app
USER kronos

EXPOSE 8000

# 기본: 로컬 대시보드. (스케줄러/봇은 compose 에서 별도 서비스로 추가 예정)
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
