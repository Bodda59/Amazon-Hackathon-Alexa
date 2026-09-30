"""
run.py — black-box test of the whole system.

Usage:
    python run.py "dinner, 700 kcal, no eggs, use up the spinach"
    python run.py                    # interactive prompt
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from graph import build_graph


BANNER = "=" * 72


def _fmt_meal(meal: dict) -> str:
    if not meal:
        return "  (none)"
    n = meal.get("nutrition") or {}
    lines = [
        f"  {meal['name']}  [{meal.get('verdict', '?')}]",
        f"    {n.get('calories', 0):.0f} kcal | "
        f"{n.get('protein_g', 0):.0f}g P | "
        f"{n.get('carbs_g', 0):.0f}g C | "
        f"{n.get('fat_g', 0):.0f}g F",
        f"    confidence: {meal.get('confidence', '?')}",
        f"    why: {meal.get('reason', meal.get('why', ''))}",
    ]
    return "\n".join(lines)


def _hr(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def _show_details(agent_name: str, details: dict) -> None:
    """Pretty-print one agent's details block."""
    if agent_name == "orchestrator":
        llm = details.get("llm", {})
        print(f"  user request: {details['inputs']['user_request']}")
        print(f"  LLM raw response:\n    {llm.get('raw_response', '')[:400]}")
        print(f"  parsed plan: {json.dumps(details['output']['plan'], indent=4)}")

    elif agent_name == "pantry":
        print(f"  low stock: {len(details['low_stock'])} items")
        for it in details["low_stock"]:
            print(f"    - {it['item']}: {it['quantity']}{it['unit']} "
                  f"(threshold {it['low_stock_threshold']})")
        print(f"  expiring: {len(details['expiring'])} items")
        for it in details["expiring"]:
            print(f"    - {it['item']}: {it['quantity']}{it['unit']} "
                  f"exp {it['expiry']}")

    elif agent_name == "preference_filter":
        print(f"  exclusions before: {[e['item'] for e in details['exclusions_before']]}")
        print(f"  exclusions after:  {[e['item'] for e in details['exclusions_after']]}")
        print(f"  preferences before: {len(details['preferences_before'])}")
        print(f"  preferences after:  {len(details['preferences_after'])}")
        if details["rule_changes_applied"]:
            print(f"  rule changes:")
            for c in details["rule_changes_applied"]:
                print(f"    + {c['kind']}: {c['item']}  (matched {c['pattern_matched']!r})")

    elif agent_name == "planner":
        llm = details.get("llm", {})
        print(f"  inputs: {details['inputs']['meal_type']} @ "
              f"{details['inputs']['calorie_target']} kcal, "
              f"{details['inputs']['inventory_count']} inventory items")
        print(f"  exclusions considered: {details['inputs']['exclusions']}")
        print(f"  LLM raw response (truncated):")
        print(f"    {llm.get('raw_response', '')[:600]}")
        if details["scrubbed_by_safety_net"]:
            print(f"  SAFETY NET removed:")
            for s in details["scrubbed_by_safety_net"]:
                print(f"    - from '{s['meal']}': {[i['item'] for i in s['removed']]}")
        print(f"  final proposed meals: {len(details['proposed_safe'])}")
        for m in details["proposed_safe"]:
            print(f"    - {m['name']}  ({m.get('servings', 1)} serv)")
            for i in m.get("ingredients", []):
                print(f"        · {i['quantity']}{i.get('unit','?')} {i['item']}")

    elif agent_name == "nutrition_critic":
        print(f"  meal_type: {details['meal_type']}")
        for tr in details["traces"]:
            print(f"\n  ── {tr['meal']} ──")
            print(f"     verdict: {tr['verdict']}   reason: {tr['reason']}")
            print(f"     input: {tr['input']['servings']} servings of "
                  f"{len(tr['input']['ingredients'])} ingredients")
            if tr.get("resolved_ingredients"):
                print(f"     resolved:")
                for r in tr["resolved_ingredients"]:
                    print(f"       {r['input']:<22} → {r['matched']:<30} "
                          f"{r['qty']:>8} = {r['grams']:>7}g  "
                          f"[{r['kcal']:>6.1f} kcal, {r['P']:>5.1f}P]  "
                          f"conf={r['confidence']}")
            if tr.get("unmatched"):
                print(f"     UNMATCHED:")
                for u in tr["unmatched"]:
                    print(f"       ✗ {u.get('item', u.get('input'))} — {u['reason']}")
            if tr.get("recipe_total"):
                rt = tr["recipe_total"]
                ps = tr["per_serving"]
                print(f"     recipe total: {rt['calories']:.0f} kcal, "
                      f"{rt['protein_g']:.0f}P {rt['carbs_g']:.0f}C {rt['fat_g']:.0f}F")
                print(f"     per serving:  {ps['calories']:.0f} kcal, "
                      f"{ps['protein_g']:.0f}P {ps['carbs_g']:.0f}C {ps['fat_g']:.0f}F")
                print(f"     avg confidence: {tr['avg_confidence']}")
            if tr.get("target_check"):
                tc = tr["target_check"]
                print(f"     target check (share of day: {tc['share_of_day']}):")
                for c in tc["checks"]:
                    mark = "✓" if c["pass"] else "✗"
                    print(f"       {mark} {c['macro']:<10} "
                          f"actual={c['actual']:>7}  "
                          f"target={c['target']:>7}  "
                          f"band={c['band']}")

    elif agent_name == "shopping":
        print(f"  chosen meal: {details['chosen_meal']}")
        print(f"  needs: {len(details['needs'])} ingredients")
        print(f"  covered: {len(details['diff_covered'])}")
        for c in details["diff_covered"]:
            print(f"    ✓ {c['item']}: have {c['have']}, need {c['needed']}")
        print(f"  short: {len(details['diff_short'])}")
        for s in details["diff_short"]:
            print(f"    ✗ {s['item']}: have {s['have']}, need {s['needed']}, "
                  f"missing {s['missing']}{s['unit']}")
        print(f"  restock suggestions: {len(details['restock_suggestions'])}")
        for r in details["restock_suggestions"]:
            print(f"    + {r['item']}: {r['quantity']}{r['unit']} ({r['reason']})")
        print(f"  final list: {len(details['final_shopping_list'])} items")

    elif agent_name == "presenter":
        if "chosen_meal" not in details:
            print(f"  (no chosen meal — {details.get('reason', '')})")
            return
        print(f"  chosen: {details['chosen_meal']['name']} "
            f"({details['chosen_meal']['nutrition'].get('calories', 0):.0f} kcal)")
        print(f"  LLM raw response: {details['llm']['raw_response'][:400]}")
        print(f"  image: {details['image'].get('url')}")
        print(f"  final card: {json.dumps(details['final_card'], indent=4)}")

def run(request: str) -> dict:
    graph = build_graph()
    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print("=" * 72)
    print(f"REQUEST: {request}")
    print(f"RUN ID:  {run_id}")
    print("=" * 72)

    try:
        result = graph.invoke({
            "run_id": run_id,
            "user_request": request,
            "events": [],
            "errors": [],
        })
    except Exception as e:
        print(f"\n!!! GRAPH CRASHED: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        return {"error": str(e)}

    # ── per-agent traces ────────────────────────────────────────────────
    print("\n\n########## AGENT TRACES ##########")
    for ev in result.get("events", []):
        agent = ev.get("agent", "?")
        _hr(f"[{agent.upper()}]  {ev.get('summary', '')}")
        if "details" in ev:
            _show_details(agent, ev["details"])

    # ── final ───────────────────────────────────────────────────────────
    _hr("FINAL RESPONSE")
    print(result.get("voice_summary", "(no summary)"))
    if result.get("card"):
        print(f"\n[card] {result['card'].get('title')} — "
              f"{result['card'].get('calories')} kcal")

    _hr("HEALTH")
    print(f"  errors:   {result.get('errors', [])}")
    print(f"  events:   {len(result.get('events', []))} agents ran")
    print(f"  run_id:   {run_id}")

    return result


if __name__ == "__main__":
    req = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
    if not req:
        req = input("What do you want? > ").strip()
    if not req:
        print("No request given.")
        sys.exit(1)
    run(req)