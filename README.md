# Agentic Meal & Macro Planner for Alexa+

> Tell Alexa+ your calorie or macro target. A multi-agent system builds a meal from the food you actually have, **verifies the numbers against USDA data before it ever says "verified"**, and can shop for what's missing, but only after you approve.

Built for the **Alexa+ track** of the [Build, Ship, Shape: Amazon Developer Hackathon](https://amazonappdev2026.devpost.com/). It is a self-hosted **MCP server** (Streamable HTTP) that Alexa+ can call, plus a **web simulation** of the Alexa+ experience for testing and demos.

---

## What it does

| Capability | What you can say or do |
|---|---|
| **Plan a meal to a target** | "Build me a 600 calorie dinner with at least 45 g protein." The agent composes a meal from your pantry, solves exact gram portions, and verifies the totals. |
| **Manage your inventory** | "I bought 500 grams of chicken breast, 1 kg of rice and 6 eggs." Free text is parsed into measured stock. Uncertain quantities trigger a clarifying question instead of a guess. Receipt photos and barcodes are also supported. |
| **Track daily macros** | Set daily targets, log a verified meal, and ask "what's my remaining protein today?" Logging also decrements the matching pantry stock. |
| **Learn your preferences** | Save diet and allergies, and record likes and dislikes ("I like spicy food"). The composer uses them in later plans. |
| **Shop for missing ingredients** | If the pantry can't hit the target, the agent builds a cart (mock catalog) and **waits for your explicit approval** before anything is ordered. |
| **Rich cards (MCP Apps)** | Each tool returns a spoken summary plus a card: meal, inventory, cart approval, nutrition status. |
| **Web simulation** | A browser app that simulates talking to Alexa+, for use without a device. |

## Why it's trustworthy

- **The LLM never states nutrition.** It only proposes ingredients and picks the next step. Calories and macros come from USDA FoodData Central (Open Food Facts fallback), and the portions are solved with SciPy/HiGHS.
- **A hard completion gate.** A result is `verified` only if deterministic verification passed with no rule violations. Otherwise you get an honest `best_effort` result with the closest attempt and what's off. Unverified meals are never presented as ready.
- **Bounded autonomy.** Repair loops, tool calls and wall-clock time all have limits enforced in code, and the router can only choose among actions that are legal for the current state.
- **No purchase without consent.** Shopping uses a mock catalog with approval, idempotency and audit guards. It cannot reach a real retailer.

---

## Hackathon submission info

- **Primary track:** Alexa+
- **Alexa+ requirement:** self-hosted MCP server over Streamable HTTP, implemented with the MCP Python SDK in `meal_agent/server.py` (a FastMCP server exposing four tools and four `ui://` MCP Apps resources).
- **Optional simulation:** `Web-App/` simulates the Alexa+ experience in the browser.
- **Beyond a basic wrapper:** agentic workflow with a LangGraph supervisor, state kept across sessions (SQLite, optional Redis), a purchasing flow with approval, MCP Apps cards, and neurosymbolic grounding (optional Neo4j knowledge graph plus deterministic rule checks).

---

## Quick start

### Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A free **USDA FoodData Central API key** (needed for nutrition lookups): https://fdc.nal.usda.gov/api-key-signup
- Optional: an Ollama API key (LLM routing/composition), a Neo4j instance, Redis

### 1. Install and configure

```sh
git clone <this-repo-url>
cd <repo-folder>
uv sync

cp .env.example .env     # then edit .env and add your own keys
```

**Do not commit `.env`.** It is git-ignored. Only `.env.example` (variable names, no secrets) is in the repo. Judges and other users supply their own keys. A minimal `.env` that runs the project:

```env
USDA_API_KEY=your-usda-key
```

Everything else is optional. See [Configuration](#configuration).

### 2. Start the MCP server

From the project root:

```sh
uv run python -m meal_agent.server
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`.

### 3. Start the web simulation (Alexa+ stand-in)

In a second terminal:

```sh
cd Web-App
cd backend
uv run uvicorn app:app --port 8000 --reload
```

> **Ports:** the MCP server and the command above both default to port 8000. Run the two on different ports when they are up at the same time, for example `uv run python -m meal_agent.server --port 8001`, and point the simulator at that URL.

### 4. Try it

Open the web simulation and say:

1. *"I have 500 grams of chicken breast, 1 kilogram of basmati rice, 6 eggs and some spinach."*
2. *"Plan me a 600 calorie dinner with at least 45 grams of protein."*
3. *"Log that meal."*
4. *"How many macros do I have left today?"*

Or call the tools directly with [MCP Inspector](https://github.com/modelcontextprotocol/inspector): choose **Streamable HTTP** and enter `http://127.0.0.1:8000/mcp` (VS Code users can use `.vscode/mcp.json`).

---

## MCP tools

The server exposes exactly four goal-level tools. Each returns `{status, spoken_summary, resourceUri, data}`.

| Tool | Purpose | Key arguments |
|---|---|---|
| `plan_meal` | Build and verify a meal for a calorie/macro target | `target` (exact `kcal`, `protein_g`, `carbs_g`, `fat_g` and/or bounds like `protein_g_min`, `carbs_g_max`), `meal_type`, `constraints`, `request`, `session_id` + `answer` to resume a clarification |
| `manage_inventory` | Read and change pantry stock | `action`: `list`, `expiring`, `add`, `update`, `consume`, `reserve`, `release`, `barcode`; `free_text`; optional receipt `image`; `confirm` |
| `shop_for_meal` | Build a cart for missing ingredients and handle approval | `plan_id`, `confirm`, `user_approved`, `decline`, `cart_id` |
| `nutrition_status` | Daily macros, targets, profile, preferences | `action`: `status`, `log`, `set_targets`, `set_profile`, `feedback` |

**Status values to know:** `verified` (passed deterministic verification), `needs_user` (a clarification or approval is pending), `best_effort` (no meal met every target), `budget_exhausted`, `not_configured` (a required provider is missing).

### Example calls

```jsonc
// Add stock from free text
manage_inventory { "action": "add", "free_text": "500 grams of chicken breast and 1 kg of rice" }

// Calorie + protein target
plan_meal { "target": { "kcal": 600, "protein_g_min": 45 }, "meal_type": "dinner" }

// Answer a clarification the agent asked
plan_meal { "target": { "kcal": 600 }, "session_id": "<returned id>", "answer": "I have 300 grams of spinach" }

// Set daily goals, save preferences, record a taste
nutrition_status { "action": "set_targets", "targets": { "kcal": 2200, "protein_g": 150 } }
nutrition_status { "action": "set_profile", "profile": { "diet": "halal", "allergies": ["peanut"] } }
nutrition_status { "action": "feedback", "preference": "spicy", "liked": true }

// Log a verified meal
nutrition_status { "action": "log", "plan_id": "<plan id from plan_meal>" }
```

---

## How it works

```mermaid
flowchart TD
	START([START]) --> ORCH[Orchestrator: validate state and choose action]
	ORCH -->|pantry relevant| INV[Inventory Agent]
	ORCH -->|pantry not needed| COMP[Composer Agent]
	INV -->|clarification| ASK[Ask User / persist session]
	INV --> COMP
	COMP --> VER[Nutrition Verifier: USDA + yield + solver + rules]
	VER -->|passed| FIN[Finish: verified-only guard]
	VER -->|miss and repairs remain| COMP
	VER -->|repair budget exhausted| PROC[Procurement Agent: mock cart + approval gate]
	PROC -->|await user approval| ASK
	PROC -->|cannot repair safely| FIN
	ASK --> END([END; resume with session_id + answer])
	FIN --> END
	ORCH -->|tool/time budget exhausted| STOP[Honest best-effort failure]
	STOP --> END
```

| Agent | Responsibility |
|---|---|
| **Supervisor** (`graph/supervisor.py`) | A cyclic LangGraph `StateGraph`. A deterministic state machine decides which actions are legal; an optional LLM router chooses among them, with a rule-based fallback. |
| **Inventory** (`agents/inventory.py`) | Parses spoken or typed quantities, converts units to grams, asks when a quantity is uncertain, reads receipts, looks up barcodes, handles reservations and expiry. |
| **Composer** (`agents/composer.py`) | Proposes ingredients and portion bounds, using preferences, expiring items and recipe search. Makes no nutrition claims. |
| **Verifier** (`agents/nutrition.py`) | Looks up nutrition (Neo4j, then USDA, then Open Food Facts), applies cooking-yield factors, solves portions, checks diet and allergen rules, and re-verifies after rounding. |
| **Procurement** (`agents/procurement.py`) | Computes the ingredient gap, builds a mock cart, and enforces the approval gate. |
| **Tracking** (`agents/tracking.py`) | Daily macro budget, verified meal logs, profile and preference learning. |

**State** lives in three places: LangGraph checkpoints, durable SQLite tables (users, inventory, plans, logs, carts, audit), and an optional Redis session cache. Inventory portions for verified or pending-approval plans are reserved for six hours. Every agent action is written to a tool trace with sensitive fields redacted.

---

## Configuration

Copy `.env.example` to `.env`. Never commit real credentials.

| Variable | Required | Purpose |
|---|---|---|
| `USDA_API_KEY` | **Yes** | USDA FoodData Central lookups (cached in SQLite for 30 days) |
| `HOST`, `PORT`, `APP_NAME` | No | Server bind address and name (default `127.0.0.1:8000`) |
| `ENABLE_ORCHESTRATOR_LLM` | No | `true` turns on LLM routing, composition and receipt vision |
| `OLLAMA_API_KEY` | With the LLM flag | Credentials for the model client in `services/LLMs.py` |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | No | Knowledge graph for nutrition, diet, allergen and yield rules ([setup](meal_agent/kg/README.md)) |
| `REDIS_URL`, `SESSION_TTL_SECONDS` | No | Session cache (default TTL 24 hours) |
| `MEAL_AGENT_USER_ID` | No | Local demo user id (default `local-demo-user`) |
| `MEAL_AGENT_DEBUG` | No | Include the decision trace in failure results |

**Runs without an LLM key.** With `ENABLE_ORCHESTRATOR_LLM` off, a rule-based policy routes the workflow, and the composer falls back to a pantry heuristic and recipe search. Nutrition values are still USDA-backed and verified.

---

## Project layout

```
meal_agent/
  server.py                 FastMCP server: four tools + MCP Apps resources
  graph/                    supervisor (LangGraph), specialist toolbox, completion guard, state
  agents/                   inventory, composer, nutrition verifier, procurement, tracking
  tools/                    macro math, SciPy/HiGHS solver, unit conversion, USDA/OFF client
  kg/                       optional Neo4j adapter (see kg/README.md)
  storage/                  SQLite workflow store and domain store
  adapters/retail/          retailer protocol + safe mock catalog
  ui/                       MCP Apps HTML cards
services/LLMs.py            optional Ollama model client
Web-App/                    browser simulation of the Alexa+ experience
  backend/                  ASGI backend (uvicorn app:app)
tests/                      unittest suite
```

## Tests

```sh
uv run python -m unittest discover -s tests
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `not_configured` from a tool | A required provider is missing, usually `USDA_API_KEY`. Check `.env`. |
| `address already in use` | The MCP server and the web backend are both on port 8000. Change one with `--port`. |
| Browser CORS errors | The server only allows the local origins listed in `build_asgi_app()` in `server.py`. Add yours if you serve the UI elsewhere. |
| Result is `best_effort` | No meal in your pantry met every target. Read `closest_attempt`, add stock, loosen the target, or approve a mock cart. |
| LLM features do nothing | `ENABLE_ORCHESTRATOR_LLM=true` and `OLLAMA_API_KEY` must both be set. |

## Known limitations

- **No account linking.** Requests run as a single local demo user. Do not expose this server publicly until authentication and per-user isolation exist.
- **No real checkout.** The retailer is a clearly fake local catalog. Real purchases need an approved adapter and authorization flow.
- **Single-user data.** The default `data/meal-agent.sqlite3` is for local development.
- The HTML cards are display-only shells; they are not yet connected to an interactive MCP Apps bridge or a trusted host-side approval signal.

## Roadmap

1. Alexa+ account linking and authenticated per-user identity.
2. Broader curated nutrition, yield and allergen data, and a licensing review of providers.
3. A real retailer adapter once purchase authorization requirements are met.
4. Expiry cleanup jobs, observability, and database backups.
5. Interactive MCP Apps bridge with a trusted approval signal.

## License

MIT License

Copyright (c) 2026 Mohammed Elnaggar

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
