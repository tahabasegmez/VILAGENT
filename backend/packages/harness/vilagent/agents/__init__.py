"""VILAGENT agent middleware package.

The legacy VILAGENT lead agent, factory, and skills-cache priming were removed
during the VILAGENT computer-use purge. This package now only hosts the small
set of reusable middlewares consumed by the computer-use agent. Import the
specific middleware modules directly (e.g.
``vilagent.agents.middlewares.tool_error_handling_middleware``).
"""
