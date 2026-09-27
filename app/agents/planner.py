"""Planner specialist: research question -> Plan of specific sub-questions (Agent.MD §3.2).

Prompt + output schema, no personality. The sub-question cap is enforced in code, not the
prompt (budgets live in code, §5).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import settings
from app.llm import structured
from app.schemas import Plan, SubQuestion


class _PlannerSubQ(BaseModel):
    text: str = Field(description="A specific, self-contained, searchable sub-question.")
    rationale: str = Field(description="One line: why answering this is needed for the main question.")


class _PlannerOutput(BaseModel):
    sub_questions: list[_PlannerSubQ]


_SYSTEM = (
    "Decompose the research question into distinct, specific, independently answerable "
    f"sub-questions (at most {settings.max_sub_questions}). Each must be concrete enough to search "
    "the web for. No overlap between sub-questions, no vague umbrella questions. "
    "Give a one-line rationale for each."
)


def plan(question: str, config: dict | None = None) -> Plan:
    question = (question or "").strip()[: settings.max_question_chars]
    model = structured(_PlannerOutput)
    out: _PlannerOutput = model.invoke([("system", _SYSTEM), ("human", question)], config=config)
    subs = [
        SubQuestion(
            text=(s.text or "").strip()[: settings.max_sub_question_chars],
            rationale=(s.rationale or "").strip()[: settings.max_sub_question_chars],
        )
        for s in out.sub_questions[: settings.max_sub_questions]
        if (s.text or "").strip()
    ]
    return Plan(question=question, sub_questions=subs)
