import requests

# Correct API endpoint for FoodData Central search
url = "https://api.nal.usda.gov/fdc/v1/foods/search"

params = {
    "api_key": "9dAv5KX10XAcNM4kBzA4p9g9Y08Ifr1p0TM72mAo",  # Replace with your key (or "DEMO_KEY" for testing)
    "query": "apple",
    "pageSize": 1               # Limit results to 1 item
}

response = requests.get(url, params=params)

if response.status_code == 200:
    data = response.json()
    print(f"Found {data['totalHits']} matching foods.\n")
    
    if data.get("foods"):
        food = data["foods"][0]
        print(f"Item: {food.get('description')}")
        
        # Find the energy (calories) nutrient entry
        for nutrient in food.get("foodNutrients", []):
            if "Energy" in nutrient.get("nutrientName", ""):
                name = nutrient.get("nutrientName")
                value = nutrient.get("value")
                unit = nutrient.get("unitName")
                print(f"{name}: {value} {unit}")
                break
else:
    print(f"Error {response.status_code}: {response.text}")