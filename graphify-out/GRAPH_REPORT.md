# Graph Report - Amazon-Hackathon-Alexa  (2026-10-13)

## Corpus Check
- Corpus is ~47,138 words - fits in a single context window. You may not need a graph.

## Summary
- 727 nodes · 1763 edges · 40 communities (19 shown, 21 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 195 edges (avg confidence: 0.9)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Culinary Composer & Recipe Generation
- Domain Storage & SQLite Persistence
- Nutrition Verifier Specialist
- Mock Retailer & Product Catalog
- Nutrition Target & Estimation Agent
- Daily Macro Tracking Agent
- Core Framework Dependencies
- Retailer Adapter Utilities
- Alexa+ Architecture & Patterns
- Supervisor Orchestration & Evals
- Kitchen Inventory Management Agent
- USDA FoodData API Client
- Meal Planning State Schema
- FastMCP Server & MCP Apps
- Runtime Application Configuration
- Specialist Agent Test Stubs
- Planner Decision Engine
- Retailer Adapter Protocol
- Supervisor Node Execution
- LangGraph StateGraph Workflow
- Repository Storage Interfaces
- MCP Apps Frontend Scripting
- Inventory Reservation & Storage
- MCP Apps UI Resources
- Evaluation Harness
- Project Guidance Docs
- Root Package Metadata
- Web Backend Package
- Backend Dependencies Config

## God Nodes (most connected - your core abstractions)
1. `DomainStore` - 80 edges
2. `NutritionTarget` - 53 edges
3. `WorkflowStore` - 40 edges
4. `OrchestratorTools` - 37 edges
5. `IntegrationTests` - 35 edges
6. `NutritionAgent` - 33 edges
7. `Neo4jKnowledgeGraph` - 33 edges
8. `ProcurementAgent` - 28 edges
9. `run_plan_meal()` - 27 edges
10. `InventoryAgent` - 24 edges

## Surprising Connections (you probably didn't know these)
- `Purchase Safety Guard` --semantically_similar_to--> `Human-in-the-Loop Gate`  [INFERRED] [semantically similar]
  Web-App/README.md → agentic-meal-macro-architecture.md
- `Neo4j Knowledge Graph Adapter` --semantically_similar_to--> `Knowledge Graph (Neo4j/NetworkX)`  [INFERRED] [semantically similar]
  meal_agent/kg/README.md → agentic-meal-macro-architecture.md
- `IntegrationTests` --uses--> `InventoryAgent`  [INFERRED]
  tests/test_integrations.py → meal_agent/agents/inventory.py
- `IntegrationTests` --uses--> `NutritionAgent`  [INFERRED]
  tests/test_integrations.py → meal_agent/agents/nutrition.py
- `IntegrationTests` --uses--> `TrackingAgent`  [INFERRED]
  tests/test_integrations.py → meal_agent/agents/tracking.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **MCP Goal-Level Tool Surface** — readme_plan_meal_tool, readme_manage_inventory_tool, readme_shop_for_meal_tool, readme_nutrition_status_tool [EXTRACTED 1.00]
- **Orchestrator-Worker Supervision Pattern** — agentic_meal_macro_architecture_orchestrator_agent, agentic_meal_macro_architecture_inventory_agent, agentic_meal_macro_architecture_culinary_composer, agentic_meal_macro_architecture_nutrition_verifier, agentic_meal_macro_architecture_procurement_agent [EXTRACTED 1.00]
- **MCP Apps UI Card Set** — ui_meal_card_meal_card_ui, ui_cart_approval_cart_approval_ui, ui_inventory_card_inventory_card_ui, ui_nutrition_status_nutrition_status_ui [EXTRACTED 1.00]

## Communities (40 total, 21 thin omitted)

### Community 0 - "Culinary Composer & Recipe Generation"
Cohesion: 0.06
Nodes (33): CandidateIngredient, CandidateMeal, cooking_yield_rules(), _ensure_included(), _fallback_candidate(), _find_stock(), _food_role(), get_meal_history() (+25 more)

### Community 1 - "Domain Storage & SQLite Persistence"
Cohesion: 0.08
Nodes (5): DomainStore, redact(), load_items(), main(), reset_inventory()

### Community 2 - "Nutrition Verifier Specialist"
Cohesion: 0.08
Nodes (21): get_nutrition_agent(), nutrition_lookup(), verify_candidate(), verify_candidate(), IngredientPortion, MacroTotals, MCPToolResponse, MealPlanResult (+13 more)

### Community 3 - "Mock Retailer & Product Catalog"
Cohesion: 0.07
Nodes (13): MockRetailer, _tokenize(), get_procurement_agent(), ProcurementAgent, shop_for_meal(), _names_match(), _normalize_reply(), OrchestratorTools (+5 more)

### Community 4 - "Nutrition Target & Estimation Agent"
Cohesion: 0.08
Nodes (4): NutritionAgent, Neo4jKnowledgeGraph, FakeNeo4jDriver, Neo4jTests

### Community 5 - "Daily Macro Tracking Agent"
Cohesion: 0.07
Nodes (6): get_tracking_agent(), nutrition_status(), TrackingAgent, WorkflowStore, redact(), MCPSurfaceTests

### Community 6 - "Core Framework Dependencies"
Cohesion: 0.06
Nodes (19): get(), ask_llm(), call_tool_direct(), chat(), stream(), ChatIn, for_llm(), health() (+11 more)

### Community 7 - "Retailer Adapter Utilities"
Cohesion: 0.09
Nodes (5): finish(), to_grams(), _in_days(), read_source_table(), sample_items()

### Community 8 - "Alexa+ Architecture & Patterns"
Cohesion: 0.08
Nodes (26): Alexa+ Integration (MCP Client), Culinary Composer Agent, Inventory Agent, Knowledge Graph (Neo4j/NetworkX), LP Portion Solver (SciPy/HiGHS), Nutrition Verifier Agent, Orchestrator Agent, Procurement Agent (+18 more)

### Community 9 - "Supervisor Orchestration & Evals"
Cohesion: 0.09
Nodes (5): run_plan_meal(), _tools(), OrchestratorTests, choose_without_pantry(), premature_finish()

### Community 10 - "Kitchen Inventory Management Agent"
Cohesion: 0.17
Nodes (7): get_inventory_agent(), InventoryAgent, list_inventory(), manage_inventory(), InventoryImage, exercise(), exercise()

### Community 11 - "USDA FoodData API Client"
Cohesion: 0.16
Nodes (5): FoodDataClient, FoodDataError, get_food_data_client(), lookup_barcode(), nutrition_lookup()

### Community 12 - "Meal Planning State Schema"
Cohesion: 0.16
Nodes (13): MealPlanningState, _allowed_actions(), _apply_result(), _budget_reason(), finish_node(), _initial_state(), _mark_task(), _new_plan() (+5 more)

### Community 13 - "FastMCP Server & MCP Apps"
Cohesion: 0.16
Nodes (7): build_asgi_app(), cart_approval_resource(), inventory_card_resource(), main(), meal_card_resource(), nutrition_status_resource(), _read_ui()

### Community 14 - "Runtime Application Configuration"
Cohesion: 0.18
Nodes (6): Settings, _covers_all(), Recipe, _template(), _words(), write_recipe()

### Community 16 - "Planner Decision Engine"
Cohesion: 0.24
Nodes (5): _llm_decision(), _make_decision(), _message_text(), PlannerDecision, _rule_decision()

### Community 18 - "Supervisor Node Execution"
Cohesion: 0.22
Nodes (6): _best_attempt_summary(), orchestrator_node(), terminate_node(), _failure_result(), finish(), _verification_gap_score()

### Community 19 - "LangGraph StateGraph Workflow"
Cohesion: 0.33
Nodes (8): build_meal_graph(), ask_user_node(), compose_node(), inventory_node(), procure_node(), specialist_node(), verify_node(), _question_needs_inventory()

### Community 21 - "MCP Apps Frontend Scripting"
Cohesion: 0.33
Nodes (3): esc(), MCP, raw()

## Knowledge Gaps
- **14 isolated node(s):** `backend`, `amazon-hackathon-alexa`, `Project Guidance (Copilot Instructions)`, `USDA FoodData Central`, `Alexa+ Simulation Web App` (+9 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 247 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **21 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `DomainStore` connect `Domain Storage & SQLite Persistence` to `Culinary Composer & Recipe Generation`, `Nutrition Verifier Specialist`, `Mock Retailer & Product Catalog`, `Nutrition Target & Estimation Agent`, `Daily Macro Tracking Agent`, `Retailer Adapter Utilities`, `Kitchen Inventory Management Agent`, `USDA FoodData API Client`?**
  _High betweenness centrality (0.176) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `DomainStore` (e.g. with `InventoryAgent` and `NutritionAgent`) actually correct?**
  _`DomainStore` has 9 INFERRED edges - model-reasoned connections that need verification._
- **What connects `backend`, `amazon-hackathon-alexa`, `Project Guidance (Copilot Instructions)` to the rest of the system?**
  _14 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Culinary Composer & Recipe Generation` be split into smaller, more focused modules?**
  _Cohesion score 0.0639269406392694 - nodes in this community are weakly interconnected._
- **Why does `NutritionTarget` connect `Nutrition Verifier Specialist` to `Mock Retailer & Product Catalog`, `Nutrition Target & Estimation Agent`, `Retailer Adapter Utilities`, `Supervisor Orchestration & Evals`, `Meal Planning State Schema`, `FastMCP Server & MCP Apps`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Are the 15 inferred relationships involving `NutritionTarget` (e.g. with `NutritionAgent` and `verify_candidate()`) actually correct?**
  _`NutritionTarget` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Should `Domain Storage & SQLite Persistence` be split into smaller, more focused modules?**
  _Cohesion score 0.08298368298368299 - nodes in this community are weakly interconnected._