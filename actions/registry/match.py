"""Is this the same child? Name + date of birth, as Drew decided (docs/registry_design.md):

  * the date of birth must be equal, otherwise NONE (a different child);
  * EXACT: the same name tokens in any order (accents, case and hyphens ignored) -> join automatically;
  * SIMILAR: any shared token (twins, a changed surname), one name inside the other (a middle name), or
    a close spelling of the whole name or of one name (difflib ratio >= 0.85) -> ask a person in Slack;
  * otherwise NONE.
Parent email is a BACKUP signal for the workers (stage 2): it can raise the question, never join.
"""

from difflib import SequenceMatcher

from .store import name_key

EXACT, SIMILAR, NONE = "exact", "similar", "none"
RATIO = 0.85


def compare(name_a: str, name_b: str, dob_a: str, dob_b: str) -> str:
    dob_a, dob_b = str(dob_a or "")[:10], str(dob_b or "")[:10]
    if not dob_a or dob_a != dob_b:
        return NONE
    key_a, key_b = name_key(name_a), name_key(name_b)
    if not key_a or not key_b:
        return NONE
    if key_a == key_b:
        return EXACT
    tokens_a, tokens_b = set(key_a.split()), set(key_b.split())
    if tokens_a & tokens_b:
        return SIMILAR
    if SequenceMatcher(None, key_a, key_b).ratio() >= RATIO:
        return SIMILAR
    if any(_close(a, b) for a in tokens_a for b in tokens_b):
        return SIMILAR
    return NONE


def _close(a: str, b: str) -> bool:
    """One name spelt slightly differently (Katherine / Katharine); short tokens never count."""
    return min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= RATIO


def find(full_name: str, dob: str, children) -> list[tuple[str, object]]:
    """(result, child) for every child in `children` that is EXACT or SIMILAR, exact first."""
    hits = [(compare(full_name, c.full_name, dob, c.dob), c) for c in children]
    hits = [h for h in hits if h[0] != NONE]
    return sorted(hits, key=lambda h: (h[0] != EXACT, h[1].famly_child_id))
