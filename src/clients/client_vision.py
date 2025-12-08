"""
Vision-based client for VILAGENT.

This client focuses on vision and screen analysis capabilities.
"""

import asyncio
import os
import sys
import json
from contextlib import AsyncExitStack
from typing import Dict, List, Any

# Third-party imports
from dotenv import load_dotenv
from openai import OpenAI
from colorama import Fore, Style, init

# MCP Imports
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Local imports
from src.shared.utils import load_system_prompt

# Settings
load_dotenv()
init(autoreset=True)

# Servers paths
SERVERS_CONFIG = {
    "vision": "src/servers/vision/vision_server.py",
    "control": "src/servers/control/control_server.py",
}

# Model configuration
MODEL_NAME = os.getenv("VISION_MODEL", "gpt-4-vision-preview")

# Load system prompt from YAML
# Note: For vision client, we use the same system.yaml but could customize it
SYSTEM_PROMPT = load_system_prompt()


class VilagentVisionClient:
    """Vision-based client for VILAGENT."""

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print(f"{Fore.RED}Error: OPENAI_API_KEY not found in .env file.")
            sys.exit(1)

        self.client = OpenAI(api_key=api_key)

        self.sessions: Dict[str, ClientSession] = {}
        self.exit_stack = AsyncExitStack()
        self.tool_routing: Dict[str, str] = {}
        self.available_tools: List[Dict] = []

        self.messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    async def connect_to_server(self, name: str, script_path: str):
        """Connect to MCP server."""
        print(f"{Fore.CYAN}🔌 Connecting to server: {name}...")

        python_exe = sys.executable
        server_params = StdioServerParameters(
            command=python_exe,
            args=[script_path],
            env=os.environ.copy()
        )

        try:
            stdio_ctx = stdio_client(server_params)
            read_stream, write_stream = await self.exit_stack.enter_async_context(stdio_ctx)

            session_ctx = ClientSession(read_stream, write_stream)
            session = await self.exit_stack.enter_async_context(session_ctx)
            await session.initialize()

            self.sessions[name] = session
            print(f"{Fore.GREEN}✅ {name} server connected.")

        except Exception as e:
            print(f"{Fore.RED}❌ {name} connection error: {e}")

    async def initialize(self):
        """Initialize and load tools."""
        # Connect to servers
        for name, path in SERVERS_CONFIG.items():
            if os.path.exists(path):
                await self.connect_to_server(name, path)
            else:
                print(f"{Fore.YELLOW}⚠️ File not found: {path}")

        # List tools
        print(f"\n{Fore.CYAN}🛠️  Loading vision tools...")

        for server_name, session in self.sessions.items():
            result = await session.list_tools()
            for tool in result.tools:
                self.tool_routing[tool.name] = server_name

                self.available_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema
                    }
                })
                print(f"   - {tool.name} ({Fore.MAGENTA}{server_name}{Fore.RESET})")

        print(f"{Fore.GREEN}✨ Vision system ready! Model: {MODEL_NAME}\n")

    async def execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute tool on the relevant server."""
        server_name = self.tool_routing.get(tool_name)
        if not server_name:
            return f"Error: Tool '{tool_name}' not found."

        session = self.sessions[server_name]
        try:
            print(f"{Fore.YELLOW}⚙️  Executing: {tool_name}...")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)

            result = await session.call_tool(tool_name, arguments)

            if result.content:
                return result.content[0].text
            return "Success (No output)"
        except Exception as e:
            msg = f"Tool Execution Error: {str(e)}"
            print(f"{Fore.RED}{msg}")
            return msg

    async def chat_loop(self):
        """Main chat loop."""
        while True:
            try:
                user_input = input(f"{Fore.BLUE}👤 You: {Style.RESET_ALL}")
                if user_input.lower() in ["exit", "quit"]:
                    break

                self.messages.append({"role": "user", "content": user_input})

                while True:
                    # OpenAI API Call
                    response = self.client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=self.messages,
                        tools=self.available_tools,
                        tool_choice="auto"
                    )

                    msg = response.choices[0].message
                    self.messages.append(msg)

                    if not msg.tool_calls:
                        print(f"{Fore.GREEN}🤖 Vilagent Vision: {Style.RESET_ALL}{msg.content}")
                        break

                    # Process Tool Calls
                    for tool_call in msg.tool_calls:
                        func_name = tool_call.function.name
                        func_args = json.loads(tool_call.function.arguments)

                        tool_result = await self.execute_tool(func_name, func_args)

                        display_result = (tool_result[:100] + '...') if len(tool_result) > 100 else tool_result
                        print(f"Result: {display_result}{Style.RESET_ALL}")

                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result
                        })

            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except Exception as e:
                print(f"{Fore.RED}Error: {e}")

    async def run(self):
        """Run the client."""
        async with self.exit_stack:
            await self.initialize()
            await self.chat_loop()


if __name__ == "__main__":
    client = VilagentVisionClient()
    asyncio.run(client.run())

