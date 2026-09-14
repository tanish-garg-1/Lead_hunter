"""Lead score: how likely a business is to need (and pay for) a website."""

NO_REAL_WEBSITE = ("No", "Social only", "Down")


def score_lead(lead):
    points = 0
    issues = [
        i for i in (lead.get("website_issues") or "").split("; ")
        if i and not i.startswith("Not checked")
    ]

    if lead.get("has_website") in NO_REAL_WEBSITE:
        points += 40
    elif len(issues) >= 2:
        points += 25
    elif len(issues) == 1:
        points += 10

    # Busy, well-reviewed places are earning money and can afford you.
    if (lead.get("rating") or 0) >= 4.0 and (lead.get("reviews") or 0) >= 50:
        points += 20
    # Active on Instagram = cares about marketing, and you can DM them.
    if lead.get("instagram"):
        points += 10
    if lead.get("email") or lead.get("phone"):
        points += 10

    priority = "Hot" if points >= 60 else "Warm" if points >= 35 else "Cold"
    return points, priority
