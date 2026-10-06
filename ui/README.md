# MCP App resources

FastMCP serves the local HTML shells at `ui://meal-card`, `ui://inventory-card`, `ui://cart-approval`, and `ui://nutrition-status`. Each public tool advertises its resource under `_meta.ui.resourceUri` and repeats the URI in the structured response for clients following the architecture document's response contract.

These are display-only starter shells, not interactive MCP Apps yet: they do not consume tool-result data through the MCP Apps bridge and the cart page has no approval button. Checkout remains disabled. Keep spoken summaries concise, and never treat a plan request as purchase approval. Add the MCP Apps host bridge and explicit approval action only alongside server-side approval-token validation.
