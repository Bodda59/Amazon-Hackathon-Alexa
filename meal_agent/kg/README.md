# Neo4j knowledge graph

The optional Neo4j adapter is implemented in `neo4j_store.py`. It uses the official async Neo4j driver, parameterized Cypher, and reads connection values from `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, and optional `NEO4J_DATABASE`. Without those settings, the app runs as before: nutrition comes from USDA/Open Food Facts and rule checks use the curated local fallback. If Neo4j is configured but unavailable during a requested diet/allergen check, verification fails closed.

The app does not create or migrate your Neo4j schema automatically. Run [neo4j_schema.cypher](neo4j_schema.cypher) once in Neo4j Browser or `cypher-shell`, then load the foods, nutrient profiles, allergens, diets, substitutions and cooking-yield facts your application supports.

## Required graph model

- `(:Ingredient {name, canonical_name, category, allergen_data_complete, diet_data_complete})`
- `(:NutrientProfile {source, source_id, verified, kcal, protein_g, carbs_g, fat_g})` — per 100 g. Only graph profiles with `verified: true` and a non-empty source are used; otherwise the verifier asks USDA/Open Food Facts.
- `(:Allergen {name, canonical_name})`
- `(:Diet {name, canonical_name})`
- `(:CookingMethod {name, canonical_name})`
- `(:Category {name, canonical_name})`
- Optional `(:Product {sku, name, ...})` for later retailer mapping.
- Relationships: `HAS_PROFILE`, `IN_CATEGORY`, `CONTAINS_ALLERGEN`, `VIOLATES_DIET`, `SUBSTITUTES {ratio, caveat, macro_equivalent, preference_score}`, `PAIRS_WELL_WITH`, `YIELD {factor}`, and product `OF_INGREDIENT` edges.

Normalize `canonical_name` to lowercase and use the same ingredient names returned by the composer/USDA lookup. Set the completeness flags to true only when data for that ingredient has actually been reviewed; they prevent missing edges from being interpreted as proof of safety. For example, `allergen_data_complete: true` with no `CONTAINS_ALLERGEN` edges means the curator has explicitly reviewed and recorded that ingredient's known allergens.

Example relationship pattern (replace names and sourced values with your reviewed facts):

```cypher
MERGE (food:Ingredient {canonical_name: toLower($ingredient_name)})
SET food.name = $ingredient_name,
	food.category = $category,
	food.allergen_data_complete = $allergen_data_complete,
	food.diet_data_complete = $diet_data_complete
MERGE (profile:NutrientProfile {source_id: $fdc_id})
SET profile.source = 'USDA FoodData Central',
	profile.verified = true,
	profile.kcal = $kcal_per_100g,
	profile.protein_g = $protein_g_per_100g,
	profile.carbs_g = $carbs_g_per_100g,
	profile.fat_g = $fat_g_per_100g
MERGE (food)-[:HAS_PROFILE]->(profile);
```

Create diet violations explicitly, e.g. `(chicken)-[:VIOLATES_DIET]->(vegan)`, and allergen membership explicitly, e.g. `(peanut_sauce)-[:CONTAINS_ALLERGEN]->(peanut)`. A substitution can carry a caveat and ratio, but it is never treated as macro-equivalent unless the relationship explicitly says so; every replacement is looked up and re-solved by the nutrition verifier.

`tools/neo4j_tools.py` exposes health-check, ingredient-fact, diet/allergen, substitute, and yield operations for internal agents. Schema/setup examples live in `neo4j_schema.cypher`; the application only queries your database.
