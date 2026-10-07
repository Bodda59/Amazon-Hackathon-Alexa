# Alexa+ simulation host

A small web app that plays the role of Alexa+: voice + chat on the left, result cards on the right.
Your MCP server is untouched. This app is the MCP *client*.

```
Browser (mic, speech, cards) -> FastAPI backend (LLM + MCP client) -> your MCP server :8005
```

## Run it

1. Start your MCP server as usual (http://localhost:8005/mcp).
2. In this folder:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
uvicorn app:app --port 8000
```

3. Open http://localhost:8000 in **Chrome or Edge** (voice input uses the browser's speech recognition).

The status dot in the top right turns green when the backend can reach the MCP server and lists its 4 tools.

## Using AWS Bedrock instead of the Anthropic API

In `.env` set `LLM_PROVIDER=bedrock`, `AWS_REGION=...`, and `MODEL_ID` to a Bedrock model ID or
inference profile from your console. Then `pip install "anthropic[bedrock]"` and make sure AWS
credentials are configured. This also gives you an AWS service to mention for the AWS Builder challenge.

## How it works

- `POST /api/chat` runs the tool loop: the LLM sees your tools (read live from the server), asks for
  one, the backend calls it over MCP, the result goes back to the LLM. Progress streams to the browser.
- `POST /api/tool/{name}` calls one tool directly. The planner form and the card buttons use it.
- **Purchases need a human tap.** If the model tries to call `shop_for_meal` with `confirm` or
  `user_approved`, the backend forces both to false, so it can only prepare a cart. Only the
  "Approve and order" button sends `user_approved: true`. This matches your server's rule that
  shopping never happens without explicit confirmation.

## Things to know

- Tool names come from your server code: `plan_meal`, `manage_inventory`, `shop_for_meal`,
  `nutrition_status`.
- Cards read your `data` object generically: any object with kcal / protein_g / carbs_g / fat_g
  becomes macro tiles, everything else becomes a labeled list, and every card has a "Raw response"
  toggle. Once you see real output you can tune `addCard` in `frontend/index.html`.
- The cards are native, not your `ui://` MCP App pages in iframes. Hosting those needs the MCP Apps
  host protocol; the planner form on the right stands in for your MCP App button.
- Use `mcp<2`: your server imports `mcp.server.fastmcp`, which v2 renamed.
