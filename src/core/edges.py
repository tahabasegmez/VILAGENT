"""
edges.py

Conditional routing functions for LangGraph.

File responsibilities:
- Defines pure routing logic: (AgentState) -> next_node_key
- No side effects. No tool calls. No LangGraph graph wiring.
- workflow.py uses these functions to attach conditional edges.

Design:
- Explicit terminal routing
- Approval gating loop
- Recovery routing with hard stops
- Optional replan hook (if workflow sets state.scratch["force_replan"]=True)

Node keys assumed in workflow.py:
  - "initialize"
  - "plan"
  - "perceive"
  - "policy_check"
  - "act"
  - "verify"
  - "recover"
  - "waiting_approval"
  - "finalize"
  - END (LangGraph END marker in workflow.py)

If your workflow uses different node keys, adjust constants below.
"""

from __future__ import annotations

from typing import Literal

from .state import AgentState


 
# Node key constants
 

NODE_INITIALIZE = "initialize"
NODE_PLAN = "plan"
NODE_PERCEIVE = "perceive"
NODE_POLICY = "policy_check"
NODE_ACT = "act"
NODE_VERIFY = "verify"
NODE_RECOVER = "recover"
NODE_WAIT_APPROVAL = "waiting_approval"
NODE_FINALIZE = "finalize"
NODE_END = "end"  # workflow.py will map "end" -> END


 
# Routing primitives
 

def route_from_initialize(state: AgentState) -> str:
    # initialize should always go planning
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE
    return NODE_PLAN


def route_from_plan(state: AgentState) -> str:
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE
    # If planner produced plan successfully, nodes.py sets PERCEIVING
    if state.status == "PERCEIVING":
        return NODE_PERCEIVE
    # Fallback: if something unusual, recover
    return NODE_RECOVER


def route_from_perceive(state: AgentState) -> str:
    # Terminal states
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE

    # nodes.py sets POLICY_CHECK or RECOVERING
    if state.status == "POLICY_CHECK":
        return NODE_POLICY
    if state.status == "RECOVERING":
        return NODE_RECOVER

    # Unexpected: attempt recovery (safe default)
    return NODE_RECOVER


def route_from_policy_check(state: AgentState) -> str:
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE

    # Approval gate
    if state.status == "WAITING_APPROVAL":
        return NODE_WAIT_APPROVAL

    if state.status == "ACTING":
        return NODE_ACT

    # Unexpected: recover (safe default)
    return NODE_RECOVER


def route_from_waiting_approval(state: AgentState) -> str:
    """
    External system toggles state.approved=True.
    Keep looping here until approved, then re-check policy.
    """
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE

    if state.approved:
        # Clear approval requirement flag only after approved
        # nodes.py policy_check will flip requires_human_approval=False on allow
        return NODE_POLICY

    return NODE_WAIT_APPROVAL


def route_from_act(state: AgentState) -> str:
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE

    # nodes.py sets VERIFYING or RECOVERING
    if state.status == "VERIFYING":
        return NODE_VERIFY
    if state.status == "RECOVERING":
        # optional forced replan hook
        if state.scratch.get("force_replan") is True:
            return NODE_PLAN
        return NODE_RECOVER

    # Unexpected: recover
    return NODE_RECOVER


def route_from_verify(state: AgentState) -> str:
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE

    # nodes.py sets PERCEIVING (next step) or RECOVERING
    if state.status == "PERCEIVING":
        return NODE_PERCEIVE
    if state.status == "RECOVERING":
        if state.scratch.get("force_replan") is True:
            return NODE_PLAN
        return NODE_RECOVER

    return NODE_RECOVER


def route_from_recover(state: AgentState) -> str:
    if state.status in ("FAILED", "ESCALATED", "DONE"):
        return NODE_FINALIZE

    # nodes.py sets PERCEIVING after recovery attempt
    if state.status == "PERCEIVING":
        return NODE_PERCEIVE

    # If recovery decided to replan (workflow/recovery policy sets flag)
    if state.scratch.get("force_replan") is True:
        return NODE_PLAN

    # Default: try perception again; if stuck, workflow should enforce max-steps or retry exhaustion
    return NODE_PERCEIVE


def route_from_finalize(state: AgentState) -> str:
    # Always end.
    return NODE_END


 
# One-shot generic router (optional)
 

def route_by_status(state: AgentState) -> str:
    """
    Optional generic router if you want fewer conditional edge declarations.
    Not required; you can use the per-node routers above instead.

    Policy:
      - DONE/FAILED/ESCALATED -> finalize
      - WAITING_APPROVAL -> waiting_approval
      - PLANNING -> plan
      - PERCEIVING -> perceive
      - POLICY_CHECK -> policy_check
      - ACTING -> act
      - VERIFYING -> verify
      - RECOVERING -> recover
      - else -> recover
    """
    if state.status in ("DONE", "FAILED", "ESCALATED"):
        return NODE_FINALIZE
    if state.status == "WAITING_APPROVAL":
        return NODE_WAIT_APPROVAL
    if state.status == "PLANNING":
        return NODE_PLAN
    if state.status == "PERCEIVING":
        return NODE_PERCEIVE
    if state.status == "POLICY_CHECK":
        return NODE_POLICY
    if state.status == "ACTING":
        return NODE_ACT
    if state.status == "VERIFYING":
        return NODE_VERIFY
    if state.status == "RECOVERING":
        return NODE_RECOVER
    return NODE_RECOVER



# DERS NOTU (edges.py) — Koşullu yönlendirme (routing) katmanı

# Bu dosya “saf” (pure) yönlendirme fonksiyonlarını içerir.
# Saf olmasının anlamı:
# - Dış dünya ile etkileşime girmez (tool çağırmaz).
# - Yan etki üretmez.
# - Sadece AgentState’e bakıp “sonraki node anahtarı” döndürür.
#
# Neden ayrı dosya?
# - nodes.py’de iş mantığı + state mutasyonu var.
# - workflow.py’de LangGraph wiring var.
# - edges.py’de ise karar kuralları var. Böylece:
#   - Akış kuralları tek yerde görünür.
#   - Test etmek kolaydır (input state → output node).
#
# 1) Node key sabitleri
#    NODE_INITIALIZE, NODE_PLAN, ... gibi string anahtarlar.
#    workflow.py, LangGraph node isimlerini bu stringlerle register eder.
#
#    NODE_END = "end" özel bir adaptördür:
#    - workflow.py içinde bu "end" anahtarının END marker’a map edilmesi beklenir.
#      (Bu projede finalize node’dan END’e edge var.)
#
# 2) route_* fonksiyonları
#    Her fonksiyon bir önceki node’a karşılık gelir:
#    - route_from_initialize(): genelde plan’a gider; terminalse finalize.
#    - route_from_plan(): plan başarılıysa PERCEIVING → perceive.
#    - route_from_perceive(): POLICY_CHECK’e ya da RECOVER’e.
#    - route_from_policy_check(): onay gerekiyorsa WAITING_APPROVAL, yoksa ACT.
#    - route_from_waiting_approval(): approved True olana kadar burada döner.
#    - route_from_act(): başarılıysa VERIFY; recover gerekiyorsa RECOVER.
#      scratch.force_replan True ise PLAN’e dönme hook’u var.
#    - route_from_verify(): başarılıysa tekrar perceive veya plan bitti → finalize.
#    - route_from_recover(): toparlanma sonrası tekrar perceive veya replan.
#    - route_from_finalize(): her zaman end.
#
# 3) “Terminal durumları” önceliği
#    Kodda çoğu router en başta şunu yapar:
#      if status in (FAILED, ESCALATED, DONE): finalize
#    Bu, FSM’in “her yerden çıkış” kuralıdır.
#
# 4) route_by_status (opsiyonel)
#    İsterseniz workflow.py’de tek bir conditional router ile daha az edge
#    deklarasyonu yapabilirsiniz. Bu projede hem okunabilirlik hem de node-başına
#    kontrol için per-node router yaklaşımı tercih edilmiş.
#
# 5) En kritik sözleşme
#    Router’lar state.status alanına dayanır.
#    Yani nodes.py içinde her node, başarılı olduğunda bir sonraki beklenen status’u
#    set etmek zorundadır (örn perceive -> POLICY_CHECK, act -> VERIFYING, vb.).
#
# Özet: edges.py “harita”, nodes.py “motor”, workflow.py “kablolama”dır.

