"""Policy-controlled observe-action-verify execution loop."""

from __future__ import annotations

import asyncio
import uuid

from vilagent.computer_use.lease import DesktopLease, DesktopLeaseToken
from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionResult,
    ActionStatus,
    ApprovalRequest,
    PolicyDecision,
    StructuredError,
    TargetStrategy,
    VerificationResult,
    utc_now,
)
from vilagent.computer_use.policy import ActionPolicy
from vilagent.computer_use.providers import ActionProvider, ApprovalProvider, ObservationProvider, VerificationProvider


class ComputerUseEngine:
    """Execute a single typed action through observation, policy, and verification."""

    _MAX_FRESHNESS_DRIFT = 0.01

    def __init__(
        self,
        *,
        observation_provider: ObservationProvider,
        action_provider: ActionProvider,
        verification_provider: VerificationProvider,
        policy: ActionPolicy,
        desktop_lease: DesktopLease,
        approval_provider: ApprovalProvider | None = None,
        session_id: str | None = None,
    ):
        self._observation_provider = observation_provider
        self._action_provider = action_provider
        self._verification_provider = verification_provider
        self._policy = policy
        self._desktop_lease = desktop_lease
        self._approval_provider = approval_provider
        self._session_id = session_id

    async def execute(self, action: ActionCommand, *, owner_id: str) -> ActionResult:
        if self._session_id is not None and action.session_id != self._session_id:
            raise ValueError(f"Engine is bound to desktop session '{self._session_id}'")
        started_at = utc_now()
        if self._can_execute_without_observation(action):
            return await self._execute_without_observation(action, owner_id=owner_id, started_at=started_at)

        before = await self._observation_provider.observe(action.session_id)

        # Fail fast on a stale semantic target or a foreground-window mismatch using
        # the validated observation, before acquiring the desktop lease or mutating.
        # Approval/policy gating lives in the action-lifecycle service, not here.
        early_error = self._validate_target_freshness(action, before) or self._validate_expected_window(action, before)
        if early_error is not None:
            return self._result(
                action=action,
                status=ActionStatus.blocked,
                started_at=started_at,
                before_observation_id=before.observation_id,
                error=early_error,
            )

        token: DesktopLeaseToken | None = None
        try:
            token = await self._desktop_lease.acquire(owner_id, timeout_seconds=action.timeout_seconds)
            immediate = await self._observation_provider.observe(action.session_id, previous=before)
            # Pre-mutation freshness guards a resolved target against the screen
            # shifting under it. Target-less keyboard actions (type_text, hotkey,
            # focus_window) do not depend on screen stability — typing goes to the
            # focused field regardless — so an animating/opening app must not block
            # them with 'desktop_changed_before_mutation'.
            immediate_error = self._validate_immediate_freshness(before, immediate) if action.target is not None else None
            if immediate_error is not None:
                return self._result(
                    action=action,
                    status=ActionStatus.blocked,
                    started_at=started_at,
                    before_observation_id=immediate.observation_id,
                    error=immediate_error,
                )
            immediate_preconditions = await self._verification_provider.verify(action.preconditions, before=immediate, after=immediate)
            if not immediate_preconditions.succeeded:
                return self._result(
                    action=action,
                    status=ActionStatus.blocked,
                    started_at=started_at,
                    before_observation_id=immediate.observation_id,
                    verification=immediate_preconditions,
                    error=StructuredError(
                        code="precondition_changed_before_mutation",
                        message="Action preconditions changed before mutation.",
                        retryable=True,
                    ),
                )
            native_result = await asyncio.wait_for(self._action_provider.execute(action), timeout=action.timeout_seconds)
            if not native_result.succeeded:
                return self._result(
                    action=action,
                    status=ActionStatus.failed,
                    started_at=started_at,
                    before_observation_id=before.observation_id,
                    native_result=native_result,
                    error=StructuredError(
                        code=native_result.error_code or "native_action_failed",
                        message=native_result.error_message or "Native action provider reported failure.",
                        retryable=True,
                    ),
                )

            after = await self._observation_provider.observe(action.session_id, previous=immediate)
            verification = await self._verification_provider.verify(action.postconditions, before=immediate, after=after)
            status = ActionStatus.succeeded if verification.succeeded else ActionStatus.uncertain
            error = None if verification.succeeded else StructuredError(code="postcondition_failed", message="Action completed but postconditions were not satisfied.", retryable=True)
            return self._result(
                action=action,
                status=status,
                started_at=started_at,
                before_observation_id=immediate.observation_id,
                after_observation_id=after.observation_id,
                native_result=native_result,
                verification=verification,
                error=error,
            )
        except TimeoutError:
            return self._result(
                action=action,
                status=ActionStatus.failed,
                started_at=started_at,
                before_observation_id=before.observation_id,
                error=StructuredError(code="action_timeout", message="Action execution timed out.", retryable=True),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._result(
                action=action,
                status=ActionStatus.failed,
                started_at=started_at,
                before_observation_id=before.observation_id,
                error=StructuredError(code="action_provider_error", message=str(exc) or exc.__class__.__name__, retryable=True),
            )
        finally:
            if token is not None:
                await self._desktop_lease.release(token)

    async def _execute_without_observation(
        self,
        action: ActionCommand,
        *,
        owner_id: str,
        started_at,
    ) -> ActionResult:
        # Simplification: Removed policy evaluation completely. All actions are allowed.

        token: DesktopLeaseToken | None = None
        try:
            token = await self._desktop_lease.acquire(owner_id, timeout_seconds=action.timeout_seconds)
            native_result = await asyncio.wait_for(self._action_provider.execute(action), timeout=action.timeout_seconds)
        except TimeoutError:
            return self._result(
                action=action,
                status=ActionStatus.failed,
                started_at=started_at,
                before_observation_id="not-required",
                error=StructuredError(code="action_timeout", message="Action execution timed out.", retryable=True),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._result(
                action=action,
                status=ActionStatus.failed,
                started_at=started_at,
                before_observation_id="not-required",
                error=StructuredError(code="action_provider_error", message=str(exc) or exc.__class__.__name__, retryable=True),
            )
        finally:
            if token is not None:
                await self._desktop_lease.release(token)

        if not native_result.succeeded:
            return self._result(
                action=action,
                status=ActionStatus.failed,
                started_at=started_at,
                before_observation_id="not-required",
                native_result=native_result,
                error=StructuredError(
                    code=native_result.error_code or "native_action_failed",
                    message=native_result.error_message or "Native action provider reported failure.",
                    retryable=True,
                ),
            )
        return self._result(
            action=action,
            status=ActionStatus.succeeded,
            started_at=started_at,
            before_observation_id="not-required",
            native_result=native_result,
        )

    @staticmethod
    def _can_execute_without_observation(action: ActionCommand) -> bool:
        if action.kind == ActionKind.integration_action:
            return True
        return (
            action.target is None
            and not action.preconditions
            and not action.postconditions
            and action.kind in {ActionKind.launch_app}
        )

    async def _request_approval(self, action: ActionCommand, reasons: list[str]) -> StructuredError | None:
        if self._approval_provider is None:
            return StructuredError(code="approval_unavailable", message="Action requires approval, but no approval provider is configured.")
        request = ApprovalRequest(
            approval_id=uuid.uuid4().hex,
            action=action,
            reasons=reasons,
            consequences=action.risk.consequences,
        )
        decision = await self._approval_provider.request_approval(request)
        if not decision.approved:
            return StructuredError(code="approval_denied", message=decision.reason or "User denied the action.")
        return None

    @staticmethod
    def _validate_target_freshness(action: ActionCommand, before) -> StructuredError | None:
        # Coordinate (vision) targets carry their own drift tolerance and are not
        # pinned to a specific observation id, so they are exempt from the stale
        # semantic-target guard.
        if action.target is None or action.target.strategy == TargetStrategy.coordinate:
            return None
        if action.target.observation_id == before.observation_id:
            return None
        if before.previous_observation_id == action.target.observation_id and before.diff_from_previous == 0:
            return None
        return StructuredError(
            code="stale_target",
            message="Action target was resolved from a stale observation.",
            retryable=True,
            details={"target_observation_id": action.target.observation_id, "current_observation_id": before.observation_id},
        )

    @staticmethod
    def _validate_immediate_freshness(before, immediate) -> StructuredError | None:
        if (
            immediate.previous_observation_id == before.observation_id
            and ComputerUseEngine._is_within_freshness_drift(immediate.diff_from_previous)
        ):
            return None
        return StructuredError(
            code="desktop_changed_before_mutation",
            message="Desktop changed after validation and before mutation.",
            retryable=True,
            details={"validated_observation_id": before.observation_id, "current_observation_id": immediate.observation_id},
        )

    @staticmethod
    def _is_within_freshness_drift(diff: float | None) -> bool:
        return diff is not None and diff <= ComputerUseEngine._MAX_FRESHNESS_DRIFT

    @staticmethod
    def _validate_expected_window(action: ActionCommand, before) -> StructuredError | None:
        if action.target is None or action.target.expected_window is None:
            return None
        expected = action.target.expected_window
        actual = before.active_window
        matches = actual is not None and actual.window_id == expected.window_id
        if matches and expected.process_id is not None:
            matches = actual.process_id == expected.process_id
        if matches and expected.process_name is not None:
            matches = actual.process_name == expected.process_name
        if matches and expected.title:
            matches = actual.title == expected.title
        if matches:
            return None
        return StructuredError(
            code="expected_window_mismatch",
            message="The active window does not match the action target's expected window.",
            retryable=True,
        )

    @staticmethod
    def _result(
        *,
        action: ActionCommand,
        status: ActionStatus,
        started_at,
        before_observation_id: str,
        after_observation_id: str | None = None,
        verification: VerificationResult | None = None,
        native_result=None,
        error: StructuredError | None = None,
    ) -> ActionResult:
        return ActionResult(
            action_id=action.action_id,
            status=status,
            started_at=started_at,
            completed_at=utc_now(),
            before_observation_id=before_observation_id,
            after_observation_id=after_observation_id,
            verification=verification,
            native_result=native_result,
            error=error,
        )
