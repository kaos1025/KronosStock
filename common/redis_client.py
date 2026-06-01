"""Redis 연결 헬퍼.

KronosKit 과 동일 Redis 인스턴스를 공유할 수 있으므로 모든 키에
settings.redis_key_prefix('kronos:stock:')를 붙여 네임스페이스를 분리한다.
동기(sync) redis-py 8.x 클라이언트를 사용한다.
"""
from __future__ import annotations

from functools import lru_cache

import redis

from common.config import settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """프로세스 전역에서 재사용하는 동기 Redis 클라이언트."""
    return redis.Redis.from_url(
        settings.redis_url,
        password=settings.redis_password or None,
        decode_responses=True,
    )


def key(*parts: object) -> str:
    """네임스페이스가 적용된 Redis 키 생성.

    key('ohlcv', '005930') -> 'kronos:stock:ohlcv:005930'
    """
    return settings.redis_key_prefix + ":".join(str(p) for p in parts)


def ping() -> bool:
    """연결 확인(대시보드 health 체크용). 실패해도 예외 없이 False 반환."""
    try:
        return bool(get_redis().ping())
    except redis.RedisError:
        return False
