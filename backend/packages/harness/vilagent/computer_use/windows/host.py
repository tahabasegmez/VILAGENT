"""Windows Agent Host runtime boundary for VILAGENT."""

from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from vilagent.computer_use.action_store import InMemoryActionStore, JsonFileActionStore
from vilagent.computer_use.browser import (
    BrowserActionProvider,
    BrowserDOMTargetProvider,
    BrowserDomainPolicy,
    BrowserHealth,
    BrowserRuntimeActionProvider,
    BrowserRuntimeDOMTargetProvider,
    BrowserSessionRegistry,
    BrowserStateVerificationProvider,
    BrowserTabLifecycleService,
    BrowserUnavailableError,
)
from vilagent.computer_use.audit import JsonlComputerUseAuditStore
from vilagent.computer_use.engine import ComputerUseEngine
from vilagent.computer_use.ipc import AuthenticatedHostControlDispatcher, HostHeartbeatState, HostProcessStatus, LocalHostIpcClient, LocalHostIpcServer
from vilagent.computer_use.lifecycle_ownership import LifecycleOwnershipClaim
from vilagent.computer_use.models import AuditEventType, ComputerUseAuditEvent, ComputerUseHostHealth, DesktopSafetySnapshot, DesktopSafetyStatus, EmergencyStopSnapshot, TargetQuery, TargetResolutionResult, TargetStrategy
from vilagent.computer_use.orchestration import ComputerUseActionService
from vilagent.computer_use.policy import ActionPolicy, DefaultActionPolicy
from vilagent.computer_use.providers import ActionProvider, ApprovalProvider, AuditEventStore, DesktopSafetyProvider, VerificationProvider
from vilagent.computer_use.safety import EmergencyStop, HostActionProvider
from vilagent.computer_use.session import DesktopSessionService
from vilagent.computer_use.target_resolver import TargetResolver
from vilagent.computer_use.verification import ConservativeVerificationProvider, RoutedVerificationProvider
from vilagent.computer_use.windows.action import WindowsUIAActionProvider, create_stable_uia_control_resolver
from vilagent.computer_use.windows.bootstrap import create_browser_runtime, create_windows_session_service, create_windows_uia_provider
from vilagent.computer_use.windows.desktop_safety import WindowsDesktopSafetyProvider
from vilagent.computer_use.windows.hotkey import WindowsGlobalHotkeyListener
from vilagent.computer_use.windows.input import WindowsPhysicalInputProvider, WindowsRoutedActionProvider, pyautogui_click
from vilagent.computer_use.windows.target import WindowsUIATargetProvider
from vilagent.computer_use.windows.uia import WindowsUIAProvider
from vilagent.computer_use.windows.verification import WindowsUIAVerificationProvider
from vilagent.config.computer_use_config import ComputerUseConfig


class WindowsAgentHost:
    """Own host-local providers and enforce mutation safety controls."""

    def __init__(
        self,
        config: ComputerUseConfig,
        *,
        session_service: DesktopSessionService | None = None,
        uia_provider: WindowsUIAProvider | None = None,
        semantic_action_provider: ActionProvider | None = None,
        audit_store: AuditEventStore | None = None,
        action_store: InMemoryActionStore | None = None,
        emergency_stop: EmergencyStop | None = None,
        desktop_safety: DesktopSafetyProvider | None = None,
        hotkey_listener: Any | None = None,
        ipc_token: str | None = None,
        verification_provider: VerificationProvider | None = None,
        target_resolver: TargetResolver | None = None,
        screen_grabber: Any | None = None,
        physical_click_injector: Any | None = None,
        browser_dom_resolver: Any | None = None,
        browser_action_executor: Any | None = None,
        browser_runtime: Any | None = None,
    ):
        self.config = config
        if browser_runtime is None and config.browser.enabled and browser_dom_resolver is None and browser_action_executor is None:
            browser_runtime = create_browser_runtime(config)
        self._browser_runtime = browser_runtime
        self._browser_policy = BrowserDomainPolicy(config.browser.allowed_domains, allow_subdomains=config.browser.allow_subdomains)
        self.browser_lifecycle = (
            BrowserTabLifecycleService(browser_runtime, BrowserSessionRegistry(), self._browser_policy)
            if config.browser.enabled and browser_runtime is not None
            else None
        )
        self.sessions = session_service or create_windows_session_service(config, grabber=screen_grabber)
        self.uia = uia_provider or create_windows_uia_provider(config)
        self.audit_store = audit_store or JsonlComputerUseAuditStore(config.host_safety.audit_dir)
        self.lifecycle_ownership = (
            LifecycleOwnershipClaim(config.lifecycle_path)
            if config.enabled and config.lifecycle_path is not None and action_store is None
            else None
        )
        self.action_store = action_store or self._create_action_store()
        self.lifecycle_events = self.action_store.events
        self.emergency_stop = emergency_stop or EmergencyStop()
        self.desktop_safety = desktop_safety or WindowsDesktopSafetyProvider()
        self.hotkey_listener = hotkey_listener
        if self.hotkey_listener is None and config.enabled:
            self.hotkey_listener = WindowsGlobalHotkeyListener(
                config.emergency_stop_hotkey,
                on_trigger=lambda: self.engage_emergency_stop("Global emergency-stop hotkey"),
            )
        self.ipc_heartbeat = HostHeartbeatState()
        self._ipc_token = ipc_token or secrets.token_urlsafe(32)
        self._export_semaphore = asyncio.Semaphore(config.observation.max_concurrent_exports)
        self._export_attempts = defaultdict(deque)
        self.ipc_server = (
            LocalHostIpcServer(
                AuthenticatedHostControlDispatcher(
                    token=self._ipc_token,
                    heartbeat=self.ipc_heartbeat,
                    health_provider=self.health,
                    sessions_provider=self.sessions.list,
                    session_provider=self.sessions.get,
                    uia_windows_provider=self.uia.list_windows,
                    uia_find_provider=self.uia.find,
                    audit_provider=getattr(self.audit_store, "list_session", None),
                    lifecycle_events_provider=self._list_lifecycle_events,
                    lifecycle_events_wait_provider=self._wait_lifecycle_events,
                    action_provider=self._get_action,
                    approvals_provider=self._list_pending_approvals,
                    approval_provider=self._get_approval,
                    approval_decision_provider=self._decide_approval,
                    action_submit_provider=self._submit_action,
                    action_cancel_provider=self._cancel_action,
                    action_execute_provider=self._execute_action,
                    session_create_provider=self._create_session,
                    session_stop_provider=self.sessions.stop,
                    session_delete_provider=self.sessions.delete,
                    observation_provider=self._observe_session,
                    target_provider=self.resolve_target,
                    blob_export_info_provider=self._blob_export_info,
                    blob_export_provider=self._blob_export,
                    browser_health_provider=self._browser_health,
                    browser_session_create_provider=self._create_browser_session,
                    browser_sessions_provider=self._list_browser_sessions,
                    browser_session_close_provider=self._close_browser_session,
                    emergency_stop_provider=self._get_emergency_stop,
                    emergency_stop_engage_provider=self.engage_emergency_stop,
                    emergency_stop_reset_provider=self.reset_emergency_stop,
                )
            )
            if config.enabled
            else None
        )
        self.verification = verification_provider or self._create_verification_provider()
        target_providers = []
        if config.browser.enabled and browser_runtime is not None:
            target_providers.append(BrowserRuntimeDOMTargetProvider(browser_runtime, self._browser_policy))
        elif config.browser.enabled and browser_dom_resolver is not None:
            target_providers.append(BrowserDOMTargetProvider(browser_dom_resolver, self._browser_policy))
        target_providers.append(WindowsUIATargetProvider(self.uia))
        self.target_resolver = target_resolver or TargetResolver(target_providers)
        semantic = semantic_action_provider or WindowsUIAActionProvider(
            control_resolver=create_stable_uia_control_resolver(config.uia_comtypes_cache_dir)
        )
        routed = WindowsRoutedActionProvider(
            semantic,
            WindowsPhysicalInputProvider(
                enabled=config.host_safety.physical_input_enabled,
                click_injector=physical_click_injector if physical_click_injector is not None else pyautogui_click,
                injection_guard=self._physical_input_guard,
            ),
            BrowserRuntimeActionProvider(browser_runtime, self._browser_policy)
            if config.browser.enabled and browser_runtime is not None
            else BrowserActionProvider(browser_action_executor, self._browser_policy)
            if config.browser.enabled and browser_action_executor is not None
            else None,
        )
        self.actions = HostActionProvider(
            routed,
            emergency_stop=self.emergency_stop,
            audit_store=self.audit_store,
            allowed_actions=config.host_safety.allowed_actions,
            desktop_safety=self.desktop_safety,
            control_heartbeat=self.ipc_heartbeat if config.enabled else None,
            unrestricted=config.unrestricted,
        )
        self.action_service = ComputerUseActionService(
            action_store=self.action_store,
            policy=DefaultActionPolicy(),
            engine_factory=self._create_orchestration_engine,
            session_validator=self.sessions.get,
            max_actions_per_owner=config.budgets.total_actions,
        )

    async def create_engine(
        self,
        session_id: str,
        *,
        verification_provider: VerificationProvider,
        policy: ActionPolicy | None = None,
        approval_provider: ApprovalProvider | None = None,
    ) -> ComputerUseEngine:
        """Create an engine that cannot bypass this host's safety wrapper."""
        return ComputerUseEngine(
            observation_provider=self.sessions,
            action_provider=self.actions,
            verification_provider=verification_provider,
            policy=policy or DefaultActionPolicy(),
            desktop_lease=await self.sessions.get_desktop_lease(session_id),
            approval_provider=approval_provider,
            session_id=session_id,
        )

    async def initialize(self) -> None:
        if self.lifecycle_ownership is not None:
            await self.lifecycle_ownership.acquire()
        try:
            initialize = getattr(self.action_store, "initialize", None)
            if initialize is not None:
                await initialize()
            if self.hotkey_listener is not None:
                await self.hotkey_listener.start()
            if self.ipc_server is not None:
                await self.ipc_server.start()
        except Exception:
            if self.lifecycle_ownership is not None:
                await self.lifecycle_ownership.release()
            raise

    async def resolve_target(
        self,
        session_id: str,
        query: TargetQuery,
        owner=None,
        browser_session_id=None,
    ) -> TargetResolutionResult:
        if browser_session_id is not None:
            observation = await self._observe_session(session_id, owner=owner, browser_session_id=browser_session_id)
        else:
            observation = await self._latest_or_observe(session_id)
        query = await self._with_vision_screenshot_hint(session_id, query, observation)
        return await self.target_resolver.resolve(query, observation=observation)

    async def _latest_or_observe(self, session_id: str):
        try:
            return await self.sessions.get_latest_observation(session_id)
        except Exception:
            return await self.sessions.observe(session_id)

    async def _with_vision_screenshot_hint(self, session_id: str, query: TargetQuery, observation) -> TargetQuery:
        if TargetStrategy.vision not in set(query.allowed_strategies):
            return query
        if query.selector_hints.get("screenshot_base64") or query.selector_hints.get("screenshot_url"):
            return query
        screenshot_ref = observation.screenshot_ref
        if screenshot_ref is None or screenshot_ref.size_bytes > self.config.observation.max_export_bytes:
            return query
        try:
            store = await self.sessions.get_observation_store(session_id)
            data = await store.get_exportable_blob(observation.observation_id, screenshot_ref.blob_id)
        except Exception:
            return query
        hints = {
            **query.selector_hints,
            "screenshot_base64": base64.b64encode(data).decode("ascii"),
            "screenshot_media_type": screenshot_ref.media_type,
        }
        return query.model_copy(update={"selector_hints": hints})

    async def health(self) -> ComputerUseHostHealth:
        engaged, reason = await self.emergency_stop.status()
        try:
            desktop_safety = await self.desktop_safety.check()
        except Exception:
            desktop_safety = DesktopSafetySnapshot(
                status=DesktopSafetyStatus.unavailable,
                reason_code="desktop_safety_provider_failed",
            )
        heartbeat = await self.ipc_heartbeat.snapshot() if self.ipc_server is not None else None
        return ComputerUseHostHealth(
            desktop_safety=desktop_safety,
            emergency_stop_engaged=engaged,
            emergency_stop_reason=reason,
            emergency_stop_hotkey_registered=bool(getattr(self.hotkey_listener, "running", False)),
            local_ipc_listening=self.ipc_server is not None and self.ipc_server.port is not None,
            ipc_heartbeat_status=heartbeat.status.value if heartbeat is not None else None,
            last_ipc_heartbeat_at=heartbeat.last_heartbeat_at if heartbeat is not None else None,
            mutation_allowed=desktop_safety.mutation_allowed
            and not engaged
            and (heartbeat is None or heartbeat.status == HostProcessStatus.healthy),
        )

    def create_ipc_client(self) -> LocalHostIpcClient:
        if self.ipc_server is None or self.ipc_server.port is None:
            raise RuntimeError("Local host IPC is not listening")
        return LocalHostIpcClient(
            port=self.ipc_server.port,
            token=self._ipc_token,
            execution_timeout_seconds=self.config.budgets.duration_seconds + 1,
        )

    async def engage_emergency_stop(self, reason: str = "Operator emergency stop") -> None:
        await self.emergency_stop.engage(reason)
        try:
            await self._audit_stop_change(reason, engaged=True)
        except Exception:
            pass

    async def reset_emergency_stop(self, reason: str = "Operator reset") -> None:
        # Reset is fail-closed: the host remains stopped if the reset cannot be audited.
        await self._audit_stop_change(reason, engaged=False)
        await self.emergency_stop.reset()

    async def _create_orchestration_engine(self, session_id: str, policy: ActionPolicy) -> ComputerUseEngine:
        return await self.create_engine(
            session_id,
            verification_provider=self.verification,
            policy=policy,
        )

    async def _list_lifecycle_events(self, owner, session_id, after_sequence, limit):
        return await self.lifecycle_events.list(
            owner=owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def _wait_lifecycle_events(self, owner, session_id, after_sequence, limit, timeout_seconds):
        return await self.lifecycle_events.wait(
            owner=owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )

    async def _get_action(self, action_id, owner):
        return await self.action_store.get_action(action_id, owner=owner)

    async def _list_pending_approvals(self, owner):
        return await self.action_store.list_pending_approvals(owner=owner)

    async def _get_approval(self, approval_id, owner):
        return await self.action_store.get_approval(approval_id, owner=owner)

    async def _decide_approval(self, approval_id, owner, approved, decided_by, reason):
        return await self.action_store.decide_approval(
            approval_id,
            approved=approved,
            decided_by=decided_by,
            reason=reason,
            owner=owner,
        )

    async def _submit_action(self, action, owner):
        return await self.action_service.submit(action, owner=owner)

    async def _cancel_action(self, action_id, owner, reason):
        return await self.action_service.cancel(action_id, owner=owner, reason=reason)

    async def _execute_action(self, action_id, owner):
        return await self.action_service.execute(action_id, owner=owner)

    async def _create_session(self, session_id):
        return await self.sessions.create(session_id=session_id)

    async def _observe_session(self, session_id, owner=None, browser_session_id=None):
        observation = await self.sessions.observe(session_id)
        if browser_session_id is None:
            return observation
        if owner is None or self.browser_lifecycle is None:
            raise BrowserUnavailableError("Browser runtime is unavailable")
        state = await self.browser_lifecycle.observe(browser_session_id, owner)
        return observation.model_copy(deep=True, update={"browser_state": state})

    async def _blob_export_info(self, session_id, observation_id, blob_id, owner):
        store = await self.sessions.get_observation_store(session_id)
        observation = await store.get(observation_id)
        await store.get_exportable_blob(observation_id, blob_id)
        for ref in (observation.screenshot_ref, observation.ui_tree_ref):
            if ref is not None and ref.blob_id == blob_id:
                return ref
        raise RuntimeError("Export-authorized blob reference is unavailable")

    async def _blob_export(self, session_id, observation_id, blob_id, owner):
        error_code = None
        try:
            self._assert_export_rate(owner)
            if self._export_semaphore.locked():
                raise RuntimeError("blob_export_concurrency_limit")
            async with self._export_semaphore:
                ref = await self._blob_export_info(session_id, observation_id, blob_id, owner)
                if ref.size_bytes > self.config.observation.max_export_bytes:
                    raise RuntimeError("blob_export_size_limit")
                store = await self.sessions.get_observation_store(session_id)
                data = await store.get_exportable_blob(observation_id, blob_id)
                await self._audit_blob_export(session_id, succeeded=True)
                return ref, data
        except Exception as exc:
            error_code = str(exc) if str(exc).startswith("blob_export_") else "blob_export_blocked"
            await self._audit_blob_export(session_id, succeeded=False, error_code=error_code)
            raise

    async def _browser_health(self):
        if self.browser_lifecycle is None:
            return BrowserHealth(
                enabled=self.config.browser.enabled,
                healthy=False,
                active_sessions=0,
                error_code="browser_unavailable" if self.config.browser.enabled else "browser_disabled",
            )
        return await self.browser_lifecycle.health()

    async def _create_browser_session(self, url, owner):
        if self.browser_lifecycle is None:
            raise BrowserUnavailableError("Browser runtime is unavailable")
        return await self.browser_lifecycle.create(url, owner)

    async def _list_browser_sessions(self, owner):
        if self.browser_lifecycle is None:
            raise BrowserUnavailableError("Browser runtime is unavailable")
        return await self.browser_lifecycle.list(owner)

    async def _close_browser_session(self, browser_session_id, owner):
        if self.browser_lifecycle is None:
            raise BrowserUnavailableError("Browser runtime is unavailable")
        await self.browser_lifecycle.close(browser_session_id, owner)

    def _assert_export_rate(self, owner) -> None:
        now = datetime.now(UTC)
        key = (owner.thread_id, owner.run_id, owner.agent_id)
        attempts = self._export_attempts[key]
        cutoff = now - timedelta(minutes=1)
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= self.config.observation.max_exports_per_minute_per_owner:
            raise RuntimeError("blob_export_rate_limit")
        attempts.append(now)

    async def _audit_blob_export(self, session_id, *, succeeded, error_code=None):
        await self.audit_store.append(
            ComputerUseAuditEvent(
                event_id=uuid.uuid4().hex,
                event_type=AuditEventType.observation_blob_exported if succeeded else AuditEventType.observation_blob_export_blocked,
                session_id=session_id,
                succeeded=succeeded,
                error_code=error_code,
            )
        )

    async def _get_emergency_stop(self):
        engaged, reason = await self.emergency_stop.status()
        return EmergencyStopSnapshot(engaged=engaged, reason=reason)

    async def _physical_input_guard(self) -> bool:
        engaged, _ = await self.emergency_stop.status()
        if engaged:
            return False
        try:
            desktop = await self.desktop_safety.check()
            heartbeat = await self.ipc_heartbeat.snapshot() if self.config.enabled else None
        except Exception:
            return False
        return desktop.mutation_allowed and (heartbeat is None or heartbeat.status == HostProcessStatus.healthy)

    def _create_verification_provider(self) -> VerificationProvider:
        screen = ConservativeVerificationProvider()
        uia = WindowsUIAVerificationProvider(self.uia)
        routes = {"screen_changed": screen, "screen_unchanged": screen, "uia_element": uia}
        if self.config.browser.enabled and self._browser_runtime is not None:
            browser = BrowserStateVerificationProvider(self._browser_runtime, self._browser_policy)
            routes["browser_allowed_domain"] = browser
            routes["browser_dom"] = browser
        return RoutedVerificationProvider(routes)

    def _create_action_store(self) -> InMemoryActionStore:
        if self.config.enabled and self.config.lifecycle_path is not None:
            return JsonFileActionStore(self.config.lifecycle_path)
        return InMemoryActionStore()

    async def shutdown(self) -> None:
        """Stop all logical sessions and release their desktop leases."""
        if self.hotkey_listener is not None:
            await self.hotkey_listener.stop()
        await self.ipc_heartbeat.stop()
        if self.ipc_server is not None:
            await self.ipc_server.stop()
        for snapshot in await self.sessions.list():
            await self.sessions.stop(snapshot.session.session_id)
        if self.lifecycle_ownership is not None:
            await self.lifecycle_ownership.release()

    async def _audit_stop_change(self, reason: str, *, engaged: bool) -> None:
        await self.audit_store.append(
            ComputerUseAuditEvent(
                event_id=uuid.uuid4().hex,
                event_type=AuditEventType.emergency_stop_changed,
                session_id="host",
                emergency_stop_engaged=engaged,
                reason=reason,
            )
        )
