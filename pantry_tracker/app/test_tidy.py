import tidy


def test_tidy_name_capitalisation_and_units():
    assert tidy.tidy_name("SHEPHERD'S PIE") == "Shepherd's Pie"
    assert tidy.tidy_name("(LARGE)") == "(Large)"
    assert tidy.tidy_name('"LARGE"') == '"Large"'
    assert tidy.tidy_name("SALT-AND-PEPPER") == "Salt-and-Pepper"
    assert tidy.tidy_name("BREAD AND BUTTER") == "Bread and Butter"
    assert tidy.tidy_name("65G 2L 500ML") == "65g 2L 500ml"
    assert tidy.tidy_name("iPhone MIX") == "iPhone MIX"


def test_food_group_real_examples():
    cases = {
        "Creams": "Dairy & eggs", "Sausages": "Meat & fish",
        "Chicken and vegetables soup": "Tins & jars", "Pre-baked breads": "Bakery",
        "Cream cheeses": "Dairy & eggs", "Plant-based spreads": "Cooking & condiments",
        "Biscuits": "Snacks & sweets", "Chocolate-covered biscuits": "Snacks & sweets",
        "Instant noodles": "Pasta, rice & cereal", "Cow milk yogurts": "Dairy & eggs",
        "Candies": "Snacks & sweets", "Breakfast cereals": "Pasta, rice & cereal",
    }
    for category, expected in cases.items():
        assert tidy.food_group(category, "") == expected
    assert tidy.food_group("", "Mash") == "Ready meals"
    assert tidy.food_group("", "Doritos") == "Snacks & sweets"
    assert tidy.food_group("", "NATURAL SEA SALT") == "Cooking & condiments"
    assert tidy.food_group("Cream crackers", "") == "Bakery"
    assert tidy.food_group("Snacks & sweets", "Chicken") == "Snacks & sweets"
    assert tidy.food_group("Bakery", "Other") == "Bakery"
    assert tidy.food_group("Other", "Cheese") == "Other"
    assert tidy.food_group("", "ice cream") == "Snacks & sweets"
    assert tidy.food_group("Snack", "") == "Snacks & sweets"
    assert tidy.food_group("Peanut butter", "Cream") == "Cooking & condiments"
