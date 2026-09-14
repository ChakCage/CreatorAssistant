import re
from ai_editor_copilot.domain.models import AssetIndex


def search(index: AssetIndex, query: str):
    """Local lexical baseline. Does not access URIs or download assets."""
    words = set(re.findall(r"\w+", query.casefold()))
    scored = [(len(words & set(re.findall(r"\w+", (a.title + " " + a.description + " " + " ".join(a.tags)).casefold()))), a)
              for a in index.assets]
    return [a for score, a in sorted(scored, key=lambda item: (-item[0], item[1].id)) if score]
