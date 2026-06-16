"""Feedback persistence — ORM and SQL repository."""

from vilagent.persistence.feedback.model import FeedbackRow
from vilagent.persistence.feedback.sql import FeedbackRepository

__all__ = ["FeedbackRepository", "FeedbackRow"]
