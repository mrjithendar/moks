"""Whether a document may be retrieved, decided one way for search and for readiness.

Search used to apply lifecycle, realm and freshness rules in Python while readiness counted with its own SQL, and
the two disagreed: a document whose review was overdue was excluded from search under `stale_policy=exclude` but
still counted as searchable. Both now read these rules.

A document is eligible for retrieval when, in this order:
  1. its row is active and in this process's realm, and so is its source        (lifecycle, realm)
  2. its lifecycle is active: draft and quarantined wait for review             (review)
  3. it is not stale under a source whose policy excludes stale documents       (freshness)
  4. the active index holds it at its current revision                          (index)
Superseded and deprecated documents stay eligible: search returns them labeled and ranked below current ones,
because a reader may need to know what was replaced. Access (grants, the request's environment and namespace)
is applied per person on top of this and never widened by it.

`freshness` is the per-row rule search applies; `stale_sql` is the same rule as one SQL expression so readiness
counts many documents in one grouped query instead of one query per document.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import DateTime, and_, case, func, literal, or_
from sqlalchemy.sql.elements import ColumnElement

from ..models import Document, KnowledgeSource
from . import access

RETIRED = ("superseded", "deprecated")


def freshness(s: KnowledgeSource, d: Document, now: datetime) -> tuple[str, str | None]:
    """current | stale | superseded | deprecated, and why. Age proves nothing unless the source says how old is too old."""
    if d.review_due_at and now > d.review_due_at and d.doc_state not in RETIRED:
        return "stale", "this document's review is overdue"
    if d.doc_state == "superseded":
        return "superseded", f"replaced by {d.superseded_by}" if d.superseded_by else "marked superseded"
    if d.doc_state == "deprecated":
        return "deprecated", "marked deprecated in the document"
    if s.review_interval_days and (s.last_reviewed_at is None or now - s.last_reviewed_at > timedelta(days=s.review_interval_days)):
        return "stale", f"the source's {s.review_interval_days}-day review is overdue"
    if s.stale_after_days and (d.checked_at is None or now - d.checked_at > timedelta(days=s.stale_after_days)):
        return "stale", f"not confirmed against its source in {s.stale_after_days} days"
    return "current", None


def excluded(s: KnowledgeSource, d: Document, now: datetime) -> str | None:
    """Why search would leave this row out, or None. The caller has already joined it to its source."""
    if d.status != "active" or s.lifecycle != "active" or d.realm != access.realm() or s.realm != access.realm():
        return "lifecycle"
    if d.lifecycle != "active":
        return "review" if d.lifecycle in ("draft", "quarantined") else "lifecycle"
    state, _ = freshness(s, d, now)
    if state == "stale" and s.stale_policy == "exclude":
        return "freshness"
    return None


def readable_sql() -> list[ColumnElement]:
    """Rule 1 and 2, as search's WHERE clause has them."""
    return [Document.status == "active", Document.lifecycle == "active", KnowledgeSource.lifecycle == "active",
            Document.realm == access.realm(), KnowledgeSource.realm == access.realm()]


def stale_sql(now: datetime) -> ColumnElement:
    """`freshness(...) == "stale"` for a Document joined to its KnowledgeSource."""
    days = literal(timedelta(days=1))
    now = literal(now, DateTime(timezone=True))
    retired = Document.doc_state.in_(RETIRED)
    review_overdue = and_(Document.review_due_at.is_not(None), Document.review_due_at < now)
    source_review = and_(KnowledgeSource.review_interval_days.is_not(None), KnowledgeSource.review_interval_days > 0,
                         or_(KnowledgeSource.last_reviewed_at.is_(None), KnowledgeSource.last_reviewed_at < now - days * KnowledgeSource.review_interval_days))
    unchecked = and_(KnowledgeSource.stale_after_days.is_not(None), KnowledgeSource.stale_after_days > 0,
                     or_(Document.checked_at.is_(None), Document.checked_at < now - days * KnowledgeSource.stale_after_days))
    return and_(~retired, or_(review_overdue, source_review, unchecked))


def stale_excluded_sql(now: datetime) -> ColumnElement:
    return and_(KnowledgeSource.stale_policy == "exclude", stale_sql(now))


def exclusion_sql(now: datetime) -> ColumnElement:
    """The reason `excluded` would give, as a SQL CASE: lifecycle | review | freshness | NULL."""
    return case(
        (or_(Document.status != "active", KnowledgeSource.lifecycle != "active", Document.realm != access.realm(), KnowledgeSource.realm != access.realm()), literal("lifecycle")),
        (Document.lifecycle.in_(("draft", "quarantined")), literal("review")),
        (Document.lifecycle != "active", literal("lifecycle")),
        (stale_excluded_sql(now), literal("freshness")),
        else_=literal(None),
    )


def count_if(cond: ColumnElement) -> ColumnElement:
    return func.count().filter(cond)
