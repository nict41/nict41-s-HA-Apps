"""Small, predictable tidy rules for product names and food groups."""

import re

_SMALL = {"and", "of", "with", "in", "the", "a"}
_GROUPS = [
    "Dairy & eggs", "Meat & fish", "Fruit & veg", "Bakery", "Snacks & sweets",
    "Tins & jars", "Pasta, rice & cereal", "Ready meals", "Drinks",
    "Cooking & condiments", "Other",
]
_RULES = [
    ("Ready meals", r"\b(ready meals?|lasagne|pizza|mash)\b"),
    ("Tins & jars", r"\b(tinned|tins?|canned|jars?|soups?)\b"),
    ("Snacks & sweets", r"\b(biscuits?|chocolates?|cand(?:y|ies)|crisps?|doritos|snacks?|ice cream)\b"),
    ("Bakery", r"\b(breads?|rolls?|buns?|crackers?|bakery)\b"),
    ("Pasta, rice & cereal", r"\b(pasta|noodles?|rice|cereals?|oats?)\b"),
    ("Cooking & condiments", r"\b(oil|sauce|peanut butter|spreads?|salt|pepper|condiment)\b"),
    ("Meat & fish", r"\b(meats?|sausages?|chicken|beef|pork|fish|salmon|tuna)\b"),
    ("Fruit & veg", r"\b(fruit|vegetables?|veg|apples?|bananas?|carrots?|potatoes?)\b"),
    ("Drinks", r"\b(juice|water|drinks?|tea|coffee|cola)\b"),
    ("Dairy & eggs", r"\b(milks?|yogurts?|yoghurts?|cheeses?|creams?|butter|eggs?)\b"),
]


def tidy_name(text: str) -> str:
    value = text or ""
    letters = [c for c in value if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) >= 0.8:
        word_index = 0

        def title_word(match):
            nonlocal word_index
            word = match.group(0).lower()
            if word_index and word in _SMALL:
                result = word
            else:
                parts = []
                for part_index, part in enumerate(word.split("-")):
                    head, *suffixes = re.split("(['’])", part)
                    if (word_index or part_index) and head in _SMALL:
                        title = head
                    else:
                        title = head[:1].upper() + head[1:]
                    parts.append(title + "".join(suffixes))
                result = "-".join(parts)
            word_index += 1
            return result

        value = re.sub(
            r"[^\W\d_]+(?:['’][^\W\d_]+)*(?:-[^\W\d_]+(?:['’][^\W\d_]+)*)*",
            title_word,
            value,
        )
    return re.sub(r"(?<=\d)(kg|ml|cl|g|l)\b", lambda match: "L" if match.group(1).lower() == "l" else match.group(1).lower(), value, flags=re.I)


def food_group(category: str, name: str) -> str:
    category_text = (category or "").strip()
    for group in _GROUPS:
        if category_text.casefold() == group.casefold():
            return group
    for value in (category or "", name or ""):
        for group, pattern in _RULES:
            if re.search(pattern, value, re.I):
                return group
    return "Other"
