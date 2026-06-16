"""Core domain primitives for VILAGENT computer-use execution."""

from vilagent.computer_use.action_store import InMemoryActionStore, JsonFileActionStore
from vilagent.computer_use.audit import JsonlComputerUseAuditStore
from vilagent.computer_use.engine import ComputerUseEngine
from vilagent.computer_use.lease import DesktopLease, DesktopLeaseToken
from vilagent.computer_use.lifecycle_events import InMemoryLifecycleEventStore
from vilagent.computer_use.lifecycle_ownership import LifecycleOwnershipClaim, LifecycleOwnershipError
from vilagent.computer_use.models import (
    ActionCommand,
    ActionResult,
    ActionStatus,
    BlobRef,
    ComputerUseHostHealth,
    Condition,
    DesktopSafetySnapshot,
    DesktopSafetyStatus,
    DesktopSessionSnapshot,
    DesktopSessionStatus,
    Observation,
    PolicyDecision,
    PolicyVerdict,
    RiskAssessment,
    RiskLevel,
    TargetQuery,
    TargetRef,
)
from vilagent.computer_use.observation_store import (
    BlobExportDeniedError,
    InMemoryObservationStore,
    JsonFileObservationStore,
    ObservationStorageQuotaError,
)
from vilagent.computer_use.orchestration import ComputerUseActionService
from vilagent.computer_use.policy import DefaultActionPolicy
from vilagent.computer_use.safety import DesktopSafetyState, EmergencyStop, HostActionProvider
from vilagent.computer_use.session import DesktopSessionService
from vilagent.computer_use.target_resolver import TargetResolver
from vilagent.computer_use.verification import ConservativeVerificationProvider, RoutedVerificationProvider

__all__ = [
    "ActionCommand",
    "ActionResult",
    "ActionStatus",
    "BlobRef",
    "BlobExportDeniedError",
    "ComputerUseEngine",
    "ComputerUseActionService",
    "ComputerUseHostHealth",
    "Condition",
    "ConservativeVerificationProvider",
    "DefaultActionPolicy",
    "DesktopLease",
    "DesktopLeaseToken",
    "DesktopSafetySnapshot",
    "DesktopSafetyState",
    "DesktopSafetyStatus",
    "DesktopSessionService",
    "DesktopSessionSnapshot",
    "DesktopSessionStatus",
    "EmergencyStop",
    "HostActionProvider",
    "InMemoryActionStore",
    "InMemoryLifecycleEventStore",
    "InMemoryObservationStore",
    "LifecycleOwnershipClaim",
    "LifecycleOwnershipError",
    "JsonlComputerUseAuditStore",
    "JsonFileActionStore",
    "JsonFileObservationStore",
    "Observation",
    "ObservationStorageQuotaError",
    "PolicyDecision",
    "PolicyVerdict",
    "RiskAssessment",
    "RiskLevel",
    "RoutedVerificationProvider",
    "TargetQuery",
    "TargetRef",
    "TargetResolver",
]
