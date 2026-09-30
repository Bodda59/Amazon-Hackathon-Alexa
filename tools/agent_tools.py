from langchain_core.tools import tool
from tools import kitchen_tools as kt  

# Wrap the ones each agent is allowed to call
pantry_tools = [
    tool(kt.get_inventory),
    tool(kt.update_item),
    tool(kt.deduct_ingredients),
    tool(kt.get_low_stock),
    tool(kt.get_expiring),
]

preference_tools = [
    tool(kt.get_exclusions),
    tool(kt.get_preferences),
    tool(kt.add_exclusion),
    tool(kt.add_preference),
    tool(kt.remove_rule),
]

# Planner gets NO write tools — only read-only helpers.
planner_tools = [
    tool(kt.get_context_packet),
    tool(kt.get_meal_history),
]

nutrition_tools = [
    tool(kt.lookup_food),
    tool(kt.calc_meal),
    tool(kt.check_targets),
    tool(kt.add_custom_food),
]

shopping_tools = [
    tool(kt.diff_needs),
    tool(kt.restock_suggestions),
    tool(kt.build_list),
]

purchasing_tools = [
    tool(kt.vendor_search),
    tool(kt.create_order),
    tool(kt.request_approval),
    tool(kt.create_payment),
    tool(kt.confirm_payment),
]

presenter_tools = [
    tool(kt.format_voice_summary),
    tool(kt.build_card),
    tool(kt.image_lookup),
]

workflow_tools = [
    tool(kt.load_context),
    tool(kt.save_context),
    tool(kt.log_event),
]

# Then: bind each list to its agent's LLM via `llm.bind_tools(pantry_tools)`
# or hand the list to a `ToolNode` in your StateGraph.