"""테스트가 쓰는 pydantic 어댑터."""

from typing import Annotated

from pydantic import FailFast, TypeAdapter

from team_wiki_engine.metrics.record import GapRecord

GAP_LIST: TypeAdapter[list[GapRecord]] = TypeAdapter(Annotated[list[GapRecord], FailFast()])
"""`read_json`에 넘기는 목록 어댑터의 본보기: `FailFast`를 붙인다."""
