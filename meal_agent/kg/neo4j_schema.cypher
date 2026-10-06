// Optional Neo4j schema for the meal planner.
// The application does NOT execute schema changes automatically.
// Run once in Neo4j Browser or cypher-shell, then load your own food/rule data.

CREATE CONSTRAINT ingredient_canonical_name IF NOT EXISTS
FOR (node:Ingredient) REQUIRE node.canonical_name IS UNIQUE;

CREATE CONSTRAINT nutrient_profile_source IF NOT EXISTS
FOR (node:NutrientProfile) REQUIRE node.source_id IS UNIQUE;

CREATE CONSTRAINT allergen_canonical_name IF NOT EXISTS
FOR (node:Allergen) REQUIRE node.canonical_name IS UNIQUE;

CREATE CONSTRAINT diet_canonical_name IF NOT EXISTS
FOR (node:Diet) REQUIRE node.canonical_name IS UNIQUE;

CREATE CONSTRAINT cooking_method_canonical_name IF NOT EXISTS
FOR (node:CookingMethod) REQUIRE node.canonical_name IS UNIQUE;

CREATE CONSTRAINT category_canonical_name IF NOT EXISTS
FOR (node:Category) REQUIRE node.canonical_name IS UNIQUE;

CREATE CONSTRAINT product_sku IF NOT EXISTS
FOR (node:Product) REQUIRE node.sku IS UNIQUE;

CREATE INDEX ingredient_name IF NOT EXISTS
FOR (node:Ingredient) ON (node.name);

// Data contract used by meal_agent/kg/neo4j_store.py:
// Ingredient: name, canonical_name (lowercase query key), category,
//             allergen_data_complete (boolean), diet_data_complete (boolean)
// NutrientProfile: source, source_id, verified (must be true for use),
//                  kcal, protein_g, carbs_g, fat_g (all per 100 g)
// Allergen/Diet/CookingMethod: name and canonical_name (lowercase query key)
// Category: name and canonical_name (lowercase query key)
// Relationships:
//   (Ingredient)-[:HAS_PROFILE]->(NutrientProfile)
//   (Ingredient)-[:CONTAINS_ALLERGEN]->(Allergen)
//   (Ingredient)-[:VIOLATES_DIET]->(Diet)
//   (Ingredient)-[:SUBSTITUTES {ratio, caveat, macro_equivalent, preference_score}]->(Ingredient)
//   (Ingredient)-[:PAIRS_WELL_WITH]->(Ingredient)
//   (Ingredient)-[:YIELD {factor}]->(CookingMethod)
//   (Ingredient)-[:IN_CATEGORY]->(Category)
//   (Product)-[:OF_INGREDIENT]->(Ingredient)
// Yield factor is cooked grams / raw grams. Set data-completeness flags only
// when the corresponding node/relationship coverage is sufficiently curated.
