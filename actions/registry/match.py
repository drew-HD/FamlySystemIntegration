"""Is this the same child? Name + date of birth, with Drew's rules (docs/registry_design.md):

  * the date of birth must be equal, otherwise NONE (a different child);
  * EXACT -> join automatically: the same words in any order (accents, case and hyphens ignored), or every
    word of the shorter name is in the longer one and the shorter has at least 2 words (HubSpot's
    `child_full_name` often has a middle name that Famly leaves out);
  * SIMILAR -> ask a person in Slack: at least 2 words match, or the same surname (twins). A close spelling
    (Katherine / Katharine, difflib ratio >= 0.85, 4+ letters) counts as a matching word;
  * otherwise NONE (one shared first name alone is not enough).
Parent email is a BACKUP signal for the workers: it can raise the question, never join.
"""

import unicodedata
from difflib import SequenceMatcher

EXACT, SIMILAR, NONE = "exact", "similar", "none"
RATIO = 0.85


def words(name: str) -> list[str]:
    """Name words in their order: accents removed, casefolded, hyphens as spaces."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold().replace("-", " ")
    return text.split()


def _close(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= RATIO)


def _matching_words(a: list[str], b: list[str]) -> int:
    """How many words of `a` pair off with a different word of `b` (exact or close spelling)."""
    left = list(b)
    count = 0
    for word in a:
        hit = next((w for w in left if w == word), None) or next((w for w in left if _close(word, w)), None)
        if hit is not None:
            left.remove(hit)
            count += 1
    return count


def compare(name_a: str, name_b: str, dob_a: str, dob_b: str) -> str:
    dob_a, dob_b = str(dob_a or "")[:10], str(dob_b or "")[:10]
    if not dob_a or dob_a != dob_b:
        return NONE
    words_a, words_b = words(name_a), words(name_b)
    if not words_a or not words_b:
        return NONE
    set_a, set_b = set(words_a), set(words_b)
    if set_a == set_b:
        return EXACT
    shorter, longer = (set_a, set_b) if len(set_a) <= len(set_b) else (set_b, set_a)
    if len(shorter) >= 2 and shorter <= longer:
        return EXACT
    if _matching_words(words_a, words_b) >= 2:
        return SIMILAR
    if len(words_a) >= 2 and len(words_b) >= 2 and _close(words_a[-1], words_b[-1]):
        return SIMILAR
    return NONE


def deal_child_name(deal: dict) -> str:
    """The child's name on a HubSpot deal: `child_full_name`, else first + last. Memory only."""
    full = " ".join(str(deal.get("child_full_name") or "").split())
    if full:
        return full
    return " ".join(str(deal.get(p) or "").strip() for p in ("child_first_name", "child_last_name")).strip()


def find(full_name: str, dob: str, children) -> list[tuple[str, object]]:
    """(result, child) for every child in `children` that is EXACT or SIMILAR, exact first."""
    hits = [(compare(full_name, c.full_name, dob, c.dob), c) for c in children]
    hits = [h for h in hits if h[0] != NONE]
    return sorted(hits, key=lambda h: (h[0] != EXACT, h[1].famly_child_id))
