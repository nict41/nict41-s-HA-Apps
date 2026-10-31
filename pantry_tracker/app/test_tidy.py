import tidy
import json
from pathlib import Path

import pytest


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


@pytest.mark.parametrize("case", json.loads((Path(__file__).parent / "storage_cases.json").read_text()))
def test_storage_suggestion_cases(case):
    assert tidy.storage_suggestion(case["categories_tags"], case["category"], case["name"]) == case["expected"]


@pytest.mark.parametrize(("tags", "category", "name", "expected"), [
    (["en:canned-foods", "en:meats"], "Canned meat", "Beef", "cupboard"),
    (["en:dairies", "en:biscuits"], "Biscuits", "Milk chocolate biscuit", "cupboard"),
    (["en:canned-foods", "en:frozen-peas"], "Peas", "Frozen peas", "freezer"),
    (["en:dairies"], "Milk", "", "fridge"),
    (["en:chips-and-fries"], "", "", ""),
    (["en:chips-and-fries", "en:snacks"], "", "", "cupboard"),
    ([], "Salted snacks", "", ""),
    ([], "Cream crackers", "", "cupboard"),
    ([], "", "Ice cream", "freezer"),
    ([], "", "Mash", ""),
])
def test_storage_suggestion_order_and_word_boundaries(tags, category, name, expected):
    assert tidy.storage_suggestion(tags, category, name) == expected


@pytest.mark.parametrize(
    ("category", "name", "expected"),
    [
        ("Cow milk yogurts", "Greek yogurt", "fridge"),
        ("Creams", "", "fridge"),
        ("Sausages", "", "fridge"),
        ("Cream cheeses", "", "fridge"),
        ("", "Salted crisps", "cupboard"),
        ("Instant noodles", "", "cupboard"),
        ("", "Peanut butter", "cupboard"),
        ("", "Cream crackers", "cupboard"),
    ],
)
def test_storage_suggestion_word_fallback_handles_plurals(category, name, expected):
    assert tidy.storage_suggestion([], category, name) == expected
