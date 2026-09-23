"""Decide when a command goes to the open-task agent instead of the planner's tools.

The registered tools stay first: they are fast, cheap and exact. A command is
handed over only when

* the model planner said so (intent ``open_task``), or
* it asks for something the tools cannot produce (a presentation, a Word
  document, a chart, a workbook with formulas or several sheets), even when
  the planner matched the records it needs, or
* the planner could not map it and it asks for work with a deliverable
  ("prepare a timetable", "compare our fees with ...").

Anything else the planner could not map (greetings, general questions, a poem)
stays with free conversation one level up.
"""

from __future__ import annotations

import re

from ..agents.contracts import AgentPlan

OPEN_TASK_INTENT = "open_task"

_PRESENTATION = re.compile(r"\b(pptx?|power ?points?|presentations?|slides?|slide ?decks?|decks?)\b")
_WORD = re.compile(r"\b(docx|word (?:doc|docs|document|documents|file|files|format)|ms ?word)\b")
_CHART = re.compile(r"\b(charts?|graphs?|plots?|histograms?|pie|infographics?|dashboards?|visuali[sz](?:e|ed|ation|ations))\b")
_SPREADSHEET = re.compile(r"\b(excel|xlsx|spreadsheets?|workbooks?|sheets?)\b")
_RICH_SHEET = re.compile(
    r"\b(formulas?|formulae|pivot|multiple sheets|separate sheets?|sheet (?:for|per) each|each (?:program|department|class|section|semester)|tabs?|"
    r"conditional formatting|colou?r(?:ed|s)?|highlight(?:ed|s)?|totals?|subtotals?|summary sheet)\b"
)
_WORK_VERB = re.compile(
    r"\b(create|make|prepare|build|generate|draft|design|produce|write|compose|compile|analy[sz]e|compare|plan|forecast|predict|"
    r"summari[sz]e|convert|export|format|calculate|banao|bana do|banaiye|taiyar karo|tayyar karo)\b"
    r"|बनाओ|बनाइए|तैयार करो|ತಯಾರಿಸಿ|ಮಾಡಿ"
)
_CREATE_VERB = re.compile(r"\b(create|make|prepare|build|generate|draft|design|produce|write|compose)\b")
_DELIVERABLE = re.compile(
    r"\b(files?|documents?|reports?|sheets?|spreadsheets?|workbooks?|tables?|lists?|analysis|analyses|plans?|timetables?|schedules?|"
    r"letters?|circulars?|notices?|memos?|minutes|summary|summaries|comparison|comparisons|charts?|graphs?|presentations?|slides?|"
    r"pptx?|excel|word|pdf|csv|certificates?|brochures?|newsletters?|agenda|forecast|breakdown|trends?)\b"
)


def requested_format_beyond_tools(text: str) -> str | None:
    """The kind of file asked for that no registered tool makes, if any."""

    lowered = text.lower()
    if _PRESENTATION.search(lowered):
        return "presentation"
    if _WORD.search(lowered):
        return "word_document"
    if _CHART.search(lowered):
        return "chart"
    if _SPREADSHEET.search(lowered) and _RICH_SHEET.search(lowered):
        return "rich_spreadsheet"
    return None


def asks_for_work(text: str) -> bool:
    lowered = text.lower()
    return bool(_WORK_VERB.search(lowered) and _DELIVERABLE.search(lowered))


def _unmapped(plan: AgentPlan, text: str) -> bool:
    if plan.intent == "unknown" and not plan.steps:
        return True
    if plan.intent != "document_question":
        return False
    # The deterministic planner's last resort (a question tried against the
    # documents), or a document search for a request that asks to write
    # something new ("draft a circular"), which a search cannot do.
    return plan.confidence < 0.5 or bool(_CREATE_VERB.search(text.lower()))


def route_to_open_task(text: str, plan: AgentPlan) -> str | None:
    """Why the command goes to the open-task agent, or None when the planner's plan stands."""

    if plan.intent == OPEN_TASK_INTENT:
        return "planner"
    if requested_format_beyond_tools(text):
        return "format"
    if _unmapped(plan, text) and asks_for_work(text):
        return "unmapped_work"
    return None


__all__ = ["OPEN_TASK_INTENT", "asks_for_work", "requested_format_beyond_tools", "route_to_open_task"]
