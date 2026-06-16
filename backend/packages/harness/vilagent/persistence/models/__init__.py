"""ORM model registration entry point.

Importing this module ensures all ORM models are registered with
``Base.metadata`` so Alembic autogenerate detects every table.

The actual ORM classes have moved to entity-specific subpackages:
- ``vilagent.persistence.thread_meta``
- ``vilagent.persistence.run``
- ``vilagent.persistence.feedback``
- ``vilagent.persistence.user``

``RunEventRow`` remains in ``vilagent.persistence.models.run_event`` because
its storage implementation lives in ``vilagent.runtime.events.store.db`` and
there is no matching entity directory.
"""

from vilagent.persistence.feedback.model import FeedbackRow
from vilagent.persistence.models.run_event import RunEventRow
from vilagent.persistence.run.model import RunRow
from vilagent.persistence.thread_meta.model import ThreadMetaRow
from vilagent.persistence.user.model import UserRow

__all__ = ["FeedbackRow", "RunEventRow", "RunRow", "ThreadMetaRow", "UserRow"]
