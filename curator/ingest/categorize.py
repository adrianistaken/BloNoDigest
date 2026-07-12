"""Rule-based categorization (spec §16). No AI in V1."""

import re

CHEAP_THRESHOLD = 15  # dollars

KEYWORD_RULES = {
    "family": r"\b(kids?|children|family|storytime|story time|youth|toddlers?|teens?|all ages|crafts for kids)\b",
    "music": r"\b(concerts?|live music|bands?|singers?|orchestra|jazz|acoustic|DJ|choir|symphony|open mic)\b",
    "food_drink": r"\b(food trucks?|beer|wine|brewery|breweries|restaurant|tastings?|dinner|brunch|cocktails?|bbq|barbecue)\b",
    "outdoor": r"\b(parks?|trails?|outdoors?|gardens?|patio|nature|hikes?|hiking|runs?|running|walks?|walking|bike|zoo)\b",
    "arts_culture": r"\b(art|arts|gallery|galleries|theater|theatre|museums?|exhibits?|exhibitions?|performances?|films?|movies?|poetry|dance|ballet|opera|comedy)\b",
    "market": r"\b(markets?|farmers market|craft fairs?|vendors?)\b",
    "festival": r"\b(festivals?|fairs?|fest)\b",
    "sports": r"\b(games?|match|tournament|hockey|basketball|baseball|soccer|football|volleyball|race|5k|10k|marathon)\b",
    "educational": r"\b(class|classes|workshops?|lectures?|seminars?|lessons?|book club|learn)\b",
    "community": r"\b(volunteers?|fundraisers?|charity|community|meetups?|clean-?up|blood drive)\b",
}

COMPILED_RULES = {cat: re.compile(pattern, re.IGNORECASE) for cat, pattern in KEYWORD_RULES.items()}

FREE_PATTERN = re.compile(r"\b(free|no cost|complimentary|no charge)\b", re.IGNORECASE)


def categorize(title, description, price_text="", price_min=None, starts_at=None, tags=None):
    """-> list of category slugs. Multiple categories allowed."""
    text = f"{title} {description} {' '.join(tags or [])}"
    categories = [cat for cat, pattern in COMPILED_RULES.items() if pattern.search(text)]

    if "family" in categories and re.search(r"\b(kids?|children|toddlers?|storytime|story time)\b", text, re.IGNORECASE):
        categories.append("kids")

    if FREE_PATTERN.search(price_text or "") or price_min == 0:
        categories.append("free")
    elif price_min is not None and float(price_min) <= CHEAP_THRESHOLD:
        categories.append("cheap")

    # Date night: evening, adult-leaning genre, not clearly kid-focused
    is_evening = starts_at is not None and starts_at.hour >= 17
    adult_genres = {"music", "food_drink", "arts_culture"}
    if is_evening and adult_genres & set(categories) and "family" not in categories:
        categories.append("date_night")

    if not categories:
        categories.append("other")
    return categories
