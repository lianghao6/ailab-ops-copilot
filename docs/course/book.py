"""Shared book identity; chapter content remains in lessons/l*.py."""
from dataclasses import dataclass, field
from deck import Lesson

BOOK_TITLE = "企业级故障响应 Agent · 项目阅读"
BOOK_FILENAME = "enterprise-incident-agent-v2.pdf"


@dataclass
class Book:
    chapters: list[Lesson] = field(default_factory=list)
    title: str = BOOK_TITLE
    subtitle: str = "六课合订本 · LLM、调查控制、证据、安全、评测与服务"
