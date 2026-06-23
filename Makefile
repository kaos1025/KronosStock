# KronosStock — 예측 개선 루프 진입점
# 사용 예: make loop-baseline CODES="005930 000660" NE=40 NP=50
CODES ?= 005930 000660
NE    ?= 40        # n-eval (평가 원점 수)
H     ?= 5         # horizon
NP    ?= 50        # n-paths (Monte Carlo)
CONFIRM ?= 20      # 홀드아웃 확인 윈도우 offset

.PHONY: eval loop-baseline loop-try loop-promote loop-show

## 빠른 단발 측정(리포트만)
eval:
	python -m strategy.evaluator $(CODES) --n-eval $(NE) --horizon $(H) --n-paths $(NP)

## 현재 상태를 baseline 으로 고정 (변경 전에 1회)
loop-baseline:
	python -m strategy.loop baseline $(CODES) --n-eval $(NE) --horizon $(H) --n-paths $(NP)

## 변경 후 candidate 평가 + baseline 대비 ACCEPT/REJECT 판정 (홀드아웃 확인 포함)
loop-try:
	python -m strategy.loop try $(CODES) --n-eval $(NE) --horizon $(H) --n-paths $(NP) --confirm-offset $(CONFIRM)

## candidate 가 ACCEPT 면 baseline 으로 승격
loop-promote:
	python -m strategy.loop promote

## 현재 baseline/candidate 지표 보기
loop-show:
	python -m strategy.loop show
