"""
HomeSync AI — entry point.

Usage:
    # Streamable HTTP (for Alexa+)
    python main.py --http

    # stdio (for local MCP clients / testing)
    python main.py

    # Custom port
    python main.py --http --port 9000
"""

import sys
from mcp_server.server import mcp


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="HomeSync AI MCP Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use Streamable HTTP transport (required for Alexa+ integration)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP port to listen on",
    )
    args = parser.parse_args()

    if args.http:
        print(f"HomeSync AI MCP server starting on http://0.0.0.0:{args.port}/mcp")
        mcp.run(transport="streamable-http")
    else:
        # stdio — useful for direct MCP client connections and testing
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
