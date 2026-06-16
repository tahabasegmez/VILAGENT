"""perform_browser_action — submit and execute a single browser action."""

from __future__ import annotations

from langchain_core.tools import tool


@tool("perform_browser_action")
async def perform_browser_action_tool(
    action_kind: str,
    session_id: str | None = None,
    target_selector: str | None = None,
    url: str | None = None,
    text: str | None = None,
    keys: str | None = None,
    postcondition_type: str | None = None,
    postcondition_value: str | None = None,
) -> str:
    """Execute one browser action.

    Use browser_action or click/type_text for browser work. Omit session_id; 
    VILAGENT creates/reuses the Windows session. Pass target_selector (e.g. 
    {"css": "#button-id"} or {"text": "Submit"}) when the action needs a target. 
    Optional postcondition_* lets execution verify the outcome.
    """
    import json
    import uuid

    from vilagent.computer_use.models import ActionCommand, ActionKind, ActionOwner, Condition, TargetQuery
    from vilagent.computer_use.tools._context import ensure_desktop_session, get_action_owner, get_host_control

    remote = get_host_control()
    owner = ActionOwner.model_validate(get_action_owner())
    try:
        resolved_session_id = await ensure_desktop_session(session_id)
    except Exception as exc:
        return f"Action failed: session_unavailable ({getattr(exc, 'code', exc.__class__.__name__)})"

    action_id = str(uuid.uuid4())
    args: dict = {}
    normalized_action_kind = {
        "navigate": "browser_action",
        "goto": "browser_action",
    }.get(action_kind, action_kind)
    
    if url:
        args["url"] = url
    if text:
        args["text"] = text
    if keys:
        args["keys"] = keys

    target = None
    if target_selector:
        selector_hints = {}
        try:
            parsed_selector = json.loads(target_selector)
            if isinstance(parsed_selector, dict):
                selector_hints.update(parsed_selector)
            else:
                selector_hints["selector"] = target_selector
        except json.JSONDecodeError:
            selector_hints["selector"] = target_selector
        try:
            resolved = await remote.resolve_target(
                resolved_session_id,
                TargetQuery(description=target_selector or normalized_action_kind, selector_hints=selector_hints),
                owner=owner,
            )
        except Exception as exc:
            return f"Action target resolution failed: {getattr(exc, 'code', exc.__class__.__name__)}"
            
        if isinstance(resolved, dict):
            if not resolved.get("succeeded", False):
                return f"Action target resolution failed: {resolved.get('error_code', 'target_not_found')}"
            target_data = resolved.get("data", {}).get("target", resolved.get("data", {}))
            from vilagent.computer_use.models import TargetRef
            target = TargetRef.model_validate(target_data)
        else:
            if resolved.target is None:
                return "Action target resolution failed: target_not_found"
            target = resolved.target

    postconditions = []
    if postcondition_type and postcondition_value:
        postconditions.append(Condition(kind=postcondition_type, expected=postcondition_value))

    try:
        action = ActionCommand(
            action_id=action_id,
            session_id=resolved_session_id,
            kind=ActionKind(normalized_action_kind),
            target=target,
            args=args,
            postconditions=postconditions,
        )
    except Exception as exc:
        return f"Action payload invalid: {exc}"

    try:
        stored = await remote.submit_action(action, owner)
    except Exception as exc:
        return f"Action submission failed: {getattr(exc, 'code', exc.__class__.__name__)}"

    if isinstance(stored, dict):
        if not stored.get("succeeded", False):
            error = stored.get("error_code", "submit_failed")
            detail = stored.get("error_message", "")
            return f"Action submission failed: {error}. {detail}".strip()
        stored_data = stored.get("data", {})
        status = stored_data.get("status", "unknown")
        approval_id = stored_data.get("approval_id", "")
    else:
        status = stored.status.value
        approval_id = stored.approval_id or ""

    if status == "awaiting_approval":
        return f"Action {action_id} requires approval (approval_id={approval_id}). Waiting for operator."

    if status in ("approved", "pending"):
        try:
            executed = await remote.execute_action(action_id, owner)
        except Exception as exc:
            return f"Action execution failed: {getattr(exc, 'code', exc.__class__.__name__)}"

        if isinstance(executed, dict):
            if not executed.get("succeeded", False):
                return f"Action execution failed: {executed.get('error_code', 'execution_failed')}"
            result = executed.get("data", {})
            final_status = result.get("status", "unknown")
            verification = result.get("verification_result", "")
            err = result.get("error_code", "")
        else:
            final_status = executed.status.value
            action_result = executed.result
            verification = ""
            err = ""
            if action_result is not None:
                final_status = action_result.status.value
                if action_result.verification is not None:
                    verification = f"verified={action_result.verification.succeeded}"
                if action_result.error is not None:
                    err = action_result.error.code

        if final_status == "succeeded":
            return f"Action {normalized_action_kind} succeeded. {verification}"
        if final_status == "failed":
            return f"Action {normalized_action_kind} failed: {err}. {verification}"
        if final_status == "uncertain":
            return f"Action {normalized_action_kind} outcome uncertain. Manual check needed."
        return f"Action {normalized_action_kind} status: {final_status}"

    deny_reason = ""
    if not isinstance(stored, dict) and stored.error is not None:
        deny_reason = stored.error.message
    return f"Action {normalized_action_kind} was {status}. {deny_reason}".strip()
