# Project guidance

- Follow the architecture in `agentic-meal-macro-architecture.md`.
- This repository uses the official MCP Python SDK v1 API (`mcp<2`); consult the v1 documentation at https://py.sdk.modelcontextprotocol.io/v1/ and the official SDK repository at https://github.com/modelcontextprotocol/python-sdk/tree/v1.x before changing server transport or FastMCP APIs.
- Expose only goal-level MCP tools. Keep local VS Code development on stdio and remote Alexa+ deployment on Streamable HTTP.
- Treat LLM responses as proposals. Nutrition arithmetic, portion verification, dietary constraints, purchase approval, and checkout guards must be deterministic and tested.
- Never enable real checkout without explicit user approval, server-validated approval tokens, budget limits, idempotency, and audit logging.
- Use `uv` for project and test commands. Preserve the existing Python requirement and dependency lock unless a dependency change is necessary.
