"""VILAGENT tool package.

The legacy VILAGENT tool assembly (``get_available_tools``) and the
config-resolved research/sandbox/community/MCP tool surface were removed during
the computer-use purge. The computer-use agent builds its own tools under
``vilagent.computer_use.tools`` and only consumes
``vilagent.tools.builtins.ask_clarification_tool`` from here.
"""

__all__: list[str] = []
