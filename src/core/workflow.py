"""
workflow.py

Scalable LangGraph integration layer.

What changed (vs previous):
- Introduced explicit ToolingConfig to avoid hardcoded tool names in nodes.
- Loads local LangGraph tools from tools.py (get_local_tools()).
- Adds MCP alias mapping as the primary mechanism for remote tools.
- Uses a persistent per-run executor stored in state.scratch to preserve idempotency cache.
- Supports modular server domains (vision_server, mouse_server, keyboard_server, uia_server, rag_server).

nodes.py will be updated next to consume ToolingConfig (injection) rather than literals.

Files:
- state.py: schemas
- nodes.py: node logic (will be updated next)
- edges.py: routing
- tools.py: LOCAL ONLY tools (wait/ping/etc.)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from langgraph.graph import StateGraph, END

from .state import AgentState, ErrorCode, ToolCall, ToolResult
from . import nodes as N
from . import edges as E
from .local_tools import get_local_tools


  
# Tooling configuration (scalable)
  

@dataclass(frozen=True)
class ToolingConfig:
    """
    Central place to define tool aliases used by nodes.
    Nodes should refer to these aliases only.
    Executor will route alias -> local or MCP fully qualified tool.

    Keep these aliases stable; change mappings in MCPMap instead.
    """
    # Vision
    screen_capture: str = "screen_capture"
    omniparser_v2_parse: str = "omniparser_v2_parse"
    screenshot_diff: str = "screenshot_diff"  # optional

    # UIA
    focus_window: str = "focus_window"
    uia_tree: str = "uia_tree"
    uia_click: str = "uia_click"
    uia_set_text: str = "uia_set_text"

    # Mouse/Keyboard
    click: str = "click"
    double_click: str = "double_click"
    right_click: str = "right_click"
    move: str = "move"
    drag: str = "drag"
    scroll: str = "scroll"

    type_text: str = "type_text"
    hotkey: str = "hotkey"
    key_down: str = "key_down"
    key_up: str = "key_up"

    # Local utility
    wait: str = "wait"
    ping: str = "ping"
    time_now_ms: str = "time_now_ms"
    clipboard_get: str = "clipboard_get"
    clipboard_set: str = "clipboard_set"


@dataclass(frozen=True)
class MCPMap:
    """
    Maps tool aliases (ToolingConfig fields) to MCP fully-qualified tool names.
    Example: "screen_capture" -> "vision_server.screen_capture"

    You can maintain multiple profiles (dev/prod) by swapping this object.
    """
    alias_to_fq: Dict[str, str] = field(default_factory=dict)

    def resolve(self, alias: str) -> Optional[str]:
        return self.alias_to_fq.get(alias)


def default_mcp_map() -> MCPMap:
    """
    MCP-first mapping (industry-standard domain split).
    Adjust fully-qualified names to match your MCP server registrations.
    """
    return MCPMap(
        alias_to_fq={
            # vision_server
            "screen_capture": "vision_server.screen_capture",
            "omniparser_v2_parse": "vision_server.omniparser_v2_parse",
            "screenshot_diff": "vision_server.screenshot_diff",

            # mouse_server
            "click": "mouse_server.click",
            "double_click": "mouse_server.double_click",
            "right_click": "mouse_server.right_click",
            "move": "mouse_server.move",
            "drag": "mouse_server.drag",
            "scroll": "mouse_server.scroll",

            # keyboard_server
            "type_text": "keyboard_server.type_text",
            "hotkey": "keyboard_server.hotkey",
            "key_down": "keyboard_server.key_down",
            "key_up": "keyboard_server.key_up",

            # uia_server (choose MCP or local; here MCP-first)
            "focus_window": "uia_server.focus_window",
            "uia_tree": "uia_server.uia_tree",
            "uia_click": "uia_server.uia_click",
            "uia_set_text": "uia_server.uia_set_text",

            # rag/db_server example (not used yet by nodes)
            # "rag_query": "rag_server.rag_query",
        }
    )


  
# MCP client contract
  

class MCPClient:
    """
    Must be provided by your runtime.
    """
    def call(self, tool_name: str, args: Dict[str, Any], timeout_ms: int) -> ToolResult:
        raise NotImplementedError


  
# Tool Registry + Executor
  

LocalToolFn = Callable[[Dict[str, Any]], ToolResult]


@dataclass
class ToolRegistry:
    """
    Local tools are plain python functions.
    MCP tools are resolved via MCPMap at call-time (no static dict duplication).
    """
    local_tools: Dict[str, LocalToolFn]
    mcp_map: MCPMap

    def has_local(self, alias: str) -> bool:
        return alias in self.local_tools

    def has_mcp(self, alias: str) -> bool:
        return self.mcp_map.resolve(alias) is not None

    def has(self, alias: str) -> bool:
        return self.has_local(alias) or self.has_mcp(alias)


class DefaultToolExecutor:
    """
    Scalable executor:
    - enforces policy allow/deny from state.policy
    - maintains idempotency cache for the run (stored in state.scratch)
    - routes alias to local tool OR MCP fully qualified tool via registry.mcp_map
    """
    def __init__(self, state: AgentState, registry: ToolRegistry, mcp: Optional[MCPClient]) -> None:
        self._state = state
        self._registry = registry
        self._mcp = mcp
        self._idem: Dict[str, ToolResult] = {}
        self._lock = threading.Lock()

    def has(self, tool_alias: str) -> bool:
        return self._registry.has(tool_alias)

    def _policy_allows(self, tool_alias: str) -> ToolResult:
        pol = self._state.policy
        if pol.tool_allowlist and tool_alias not in pol.tool_allowlist:
            pol.last_decision = "DENY"
            pol.deny_reason = "Tool not in allowlist"
            return ToolResult(ok=False, error=ErrorCode.POLICY_DENY_ALLOWLIST)
        if tool_alias in pol.tool_denylist:
            pol.last_decision = "DENY"
            pol.deny_reason = "Tool in denylist"
            return ToolResult(ok=False, error=ErrorCode.POLICY_DENY_DENYLIST)
        pol.last_decision = "ALLOW"
        pol.deny_reason = None
        return ToolResult(ok=True, data=None)

    def call(self, call: ToolCall) -> ToolResult:
        # Policy
        pol = self._policy_allows(call.name)
        if not pol.ok:
            self._state.telemetry.event("tool_denied", tool=call.name, reason=pol.error)
            return pol

        # Idempotency
        with self._lock:
            hit = self._idem.get(call.idempotency_key)
            if hit is not None:
                self._state.telemetry.event("tool_idempotent_hit", tool=call.name)
                return hit

        # Dispatch
        res: ToolResult
        try:
            if call.name in self._registry.local_tools:
                fn = self._registry.local_tools[call.name]
                res = fn(call.args)

            else:
                fq = self._registry.mcp_map.resolve(call.name)
                if fq is None:
                    res = ToolResult(ok=False, error=f"{ErrorCode.TOOL_NOT_FOUND}: {call.name}")
                elif self._mcp is None:
                    res = ToolResult(ok=False, error=ErrorCode.MCP_NOT_CONFIGURED)
                else:
                    res = self._mcp.call(fq, call.args, timeout_ms=call.timeout_ms)

        except Exception as e:
            res = ToolResult(ok=False, error=f"{type(e).__name__}: {e}")

        # Cache (cache failures too to avoid repeated harmful retries)
        with self._lock:
            self._idem[call.idempotency_key] = res

        self._state.telemetry.event("tool_called", tool=call.name, ok=res.ok)
        return res


  
# Dependency bundle
  

@dataclass(frozen=True)
class RuntimeDeps:
    planner: N.PlannerFn
    action_selector: N.ActionSelectorFn
    verifier: N.VerifierFn
    recovery: N.RecoveryFn

    tooling: ToolingConfig = ToolingConfig()
    registry: ToolRegistry = field(default_factory=lambda: ToolRegistry(local_tools=get_local_tools(), mcp_map=default_mcp_map()))
    mcp_client: Optional[MCPClient] = None

    # Perception payload strategy (nodes will use this next)
    store_screenshot_b64: bool = False  # MCP vision_server should support image_hash resolution if False


  
# Executor lifecycle (per-run, persisted in state)
  

_EXECUTOR_KEY = "_executor_v1"


def _get_executor(state: AgentState, deps: RuntimeDeps) -> DefaultToolExecutor:
    ex = state.scratch.get(_EXECUTOR_KEY)
    if ex is None or not isinstance(ex, DefaultToolExecutor):
        ex = DefaultToolExecutor(state, deps.registry, deps.mcp_client)
        state.scratch[_EXECUTOR_KEY] = ex
    return ex


  
# Graph builder
  

def build_workflow(deps: RuntimeDeps):
    g = StateGraph(AgentState)

    # Nodes (deps-bound)
    def _initialize(state: AgentState) -> AgentState:
        return N.node_initialize(state)

    def _plan(state: AgentState) -> AgentState:
        return N.node_plan(state, deps.planner)

    def _perceive(state: AgentState) -> AgentState:
        ex = _get_executor(state, deps)
        return N.node_perceive(
            state,
            ex,
            deps.tooling,
            store_screenshot_b64=deps.store_screenshot_b64,
            prefer_uia_tree=True,
            omniparser_enabled=True,
        )

    def _policy(state: AgentState) -> AgentState:
        return N.node_policy_check(state)

    def _act(state: AgentState) -> AgentState:
        ex = _get_executor(state, deps)
        return N.node_act(
            state,
            ex,
            deps.tooling,
            deps.action_selector,
            post_action_capture=True,
        )

    def _verify(state: AgentState) -> AgentState:
        return N.node_verify(state, deps.tooling, deps.verifier)

    def _recover(state: AgentState) -> AgentState:
        ex = _get_executor(state, deps)
        return N.node_recover(state, ex, deps.tooling, deps.recovery)

    def _wait_approval(state: AgentState) -> AgentState:
        return N.node_waiting_approval(state)

    def _finalize(state: AgentState) -> AgentState:
        # Ensure state is serializable; executor must not leak outside runtime
        state.scratch.pop(_EXECUTOR_KEY, None)
        return N.node_finalize(state)

    # Register nodes
    g.add_node(E.NODE_INITIALIZE, _initialize)
    g.add_node(E.NODE_PLAN, _plan)
    g.add_node(E.NODE_PERCEIVE, _perceive)
    g.add_node(E.NODE_POLICY, _policy)
    g.add_node(E.NODE_ACT, _act)
    g.add_node(E.NODE_VERIFY, _verify)
    g.add_node(E.NODE_RECOVER, _recover)
    g.add_node(E.NODE_WAIT_APPROVAL, _wait_approval)
    g.add_node(E.NODE_FINALIZE, _finalize)

    g.set_entry_point(E.NODE_INITIALIZE)

    # Fixed edges
    g.add_edge(E.NODE_INITIALIZE, E.NODE_PLAN)
    g.add_edge(E.NODE_PLAN, E.NODE_PERCEIVE)

    # Conditional edges
    g.add_conditional_edges(E.NODE_PERCEIVE, E.route_from_perceive, {
        E.NODE_POLICY: E.NODE_POLICY,
        E.NODE_RECOVER: E.NODE_RECOVER,
        E.NODE_FINALIZE: E.NODE_FINALIZE,
    })

    g.add_conditional_edges(E.NODE_POLICY, E.route_from_policy_check, {
        E.NODE_ACT: E.NODE_ACT,
        E.NODE_WAIT_APPROVAL: E.NODE_WAIT_APPROVAL,
        E.NODE_RECOVER: E.NODE_RECOVER,
        E.NODE_FINALIZE: E.NODE_FINALIZE,
    })

    g.add_conditional_edges(E.NODE_WAIT_APPROVAL, E.route_from_waiting_approval, {
        E.NODE_POLICY: E.NODE_POLICY,
        E.NODE_WAIT_APPROVAL: E.NODE_WAIT_APPROVAL,
        E.NODE_FINALIZE: E.NODE_FINALIZE,
    })

    g.add_conditional_edges(E.NODE_ACT, E.route_from_act, {
        E.NODE_VERIFY: E.NODE_VERIFY,
        E.NODE_RECOVER: E.NODE_RECOVER,
        E.NODE_PLAN: E.NODE_PLAN,
        E.NODE_FINALIZE: E.NODE_FINALIZE,
    })

    g.add_conditional_edges(E.NODE_VERIFY, E.route_from_verify, {
        E.NODE_PERCEIVE: E.NODE_PERCEIVE,
        E.NODE_RECOVER: E.NODE_RECOVER,
        E.NODE_PLAN: E.NODE_PLAN,
        E.NODE_FINALIZE: E.NODE_FINALIZE,
    })

    g.add_conditional_edges(E.NODE_RECOVER, E.route_from_recover, {
        E.NODE_PERCEIVE: E.NODE_PERCEIVE,
        E.NODE_PLAN: E.NODE_PLAN,
        E.NODE_FINALIZE: E.NODE_FINALIZE,
    })

    g.add_edge(E.NODE_FINALIZE, END)

    return g.compile()



# (workflow.py) — LangGraph kablolama + tool dispatch (güncel yapı)

# Bu dosya, çekirdeğin “entegrasyon” katmanıdır: LangGraph ile graph kurar ve
# nodes.py fonksiyonlarını runtime bağımlılıklarla bağlayarak çalıştırılabilir hale
# getirir.
#
# Bu sürümde öne çıkan tasarım:
#
# 1) ToolingConfig (alias sözleşmesi)
#    - nodes.py içinde tool isimleri hardcode edilmez.
#    - Bunun yerine ToolingConfig içindeki alias’lar kullanılır:
#      örn tooling.screen_capture == "screen_capture".
#    - Bu alias’lar “stabil API” gibi davranır; gerçek tool’un nerede çalıştığını
#      (local mı MCP mi) ToolRegistry + MCPMap belirler.
#
# 2) MCPMap (alias -> fully-qualified MCP tool)
#    - alias_to_fq sözlüğü ile "screen_capture" -> "vision_server.screen_capture" gibi.
#    - Birden fazla profil (dev/prod) için farklı MCPMap nesneleri üretebilirsiniz.
#
# 3) ToolRegistry
#    - local_tools: Python fonksiyonları (ör. wait/ping/clipboard).
#      Bu projede local tool’lar [src/core/local_tools.py](src/core/local_tools.py) içinden gelir.
#    - mcp_map: MCP tarafı routing bilgisi.
#    - has(alias): bir alias local veya MCP’de var mı?
#
# 4) DefaultToolExecutor
#    - nodes.py’nin beklediği ToolExecutor protokolünü sağlar.
#    - 4.1 Policy enforcement:
#      state.policy.allowlist/denylist kontrol edilir; deny ise ToolResult ok=False.
#    - 4.2 Idempotency cache:
#      idempotency_key -> ToolResult cache’lenir (başarısız sonuçlar dahil).
#      Amaç: aynı eylemi retry döngüsünde “zararlı şekilde” tekrar tekrar yapmamak.
#    - 4.3 Dispatch:
#      - alias local_tools içindeyse: fn(args)
#      - değilse: mcp_map.resolve(alias) ile fq tool bulunur ve mcp_client.call(...) yapılır.
#
# 5) Executor yaşam döngüsü: state.scratch içinde kalıcılık
#    - _EXECUTOR_KEY ile executor instance’ı state.scratch içine konur.
#    - Böylece node’lar arasında idempotency cache korunur.
#    - finalize aşamasında scratch içinden temizlenir (serialization güvenliği).
#
# 6) RuntimeDeps (Dependency Injection)
#    Uygulama, şu fonksiyonları/objeleri enjekte eder:
#    - planner: AgentState -> Plan
#    - action_selector: (AgentState, ToolingConfig) -> list[ToolCall]
#    - verifier: (AgentState, ToolingConfig) -> (ok, details)
#    - recovery: (AgentState, ToolingConfig) -> list[ToolCall]
#    Ek olarak:
#    - tooling: ToolingConfig alias seti
#    - registry: local_tools + mcp_map
#    - mcp_client: MCP çağrıları için client
#    - store_screenshot_b64: algılama payload stratejisi
#
# 7) build_workflow(deps)
#    - Node wrapper’ları deps’e bağlar:
#      _perceive/_act/_verify/_recover çağrılarında deps.tooling parametresi
#      nodes.py’ye enjekte edilir.
#    - edges.py router’ları ile conditional edge’ler tanımlanır.
#
# Özet: workflow.py “kablolama + dispatch”tır; iş mantığı nodes.py’de,
# yönlendirme kuralları edges.py’dedir.

