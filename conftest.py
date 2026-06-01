"""pytest 부트스트랩 — repo 루트를 sys.path 에 올려 `inference`/`common` 패키지 import 보장.

`pytest` 든 `python -m pytest` 든 동일하게 동작하도록 한다.
"""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
