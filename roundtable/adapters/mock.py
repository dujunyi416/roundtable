"""Scripted fake adapter for tests and local smoke runs without a second CLI."""
from __future__ import annotations

from . import Adapter, Reply

DEFAULT_REPLY = (
    "(mock) I reviewed the message above and have no objections.\n\nVERDICT: APPROVE"
)


class MockAdapter(Adapter):
    name = "mock"

    def __init__(self, replies: list[str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.replies = list(replies) if replies else []
        self.prompts: list[str] = []  # every prompt received, for assertions

    def send(self, prompt: str) -> Reply:
        self.prompts.append(prompt)
        text = self.replies.pop(0) if self.replies else DEFAULT_REPLY
        self.session_id = "mock-session"
        return Reply(text=text, duration_s=0.0, session_id=self.session_id)
