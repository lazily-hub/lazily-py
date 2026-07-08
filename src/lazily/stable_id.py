"""Manufactured identity for text — ``stable_id``.

The Python counterpart of ``lazily-spec/cell-model.md`` § "Manufactured identity
for text". Markdown has no inherent node ids, so reconciliation keys are
*manufactured* from text in three layers:

- **in-band anchors** (``a:<anchor>``) — exact, survive a body rewrite;
- **content-derived hashes** (``c:<hash>``) of whitespace-normalized text —
  survive reflow/reorder, change on edit;
- **alignment** by word-LCS similarity (``>= 0.5`` ⇒ ``Edited`` / key inherited
  from the matched predecessor; below ⇒ ``Inserted``). A true rewrite
  legitimately reads as insert + remove.

Keys carry an ``a:`` / ``c:`` prefix so the anchored and content keyspaces never
collide. This is the linchpin that keeps keyed reconciliation from degrading to
whole-document replacement over unstable text — see
:mod:`lazily.reconciliation` for the move-minimized op set that consumes these
keys.

The executable reference behind the
``lazily-spec/conformance/collections/stableid_alignment.json`` conformance
fixture.
"""

from __future__ import annotations


__all__ = [
    "ANCHOR_PREFIX",
    "CONTENT_PREFIX",
    "Alignment",
    "align",
    "assign_stable_keys",
    "block_key",
    "content_hash",
    "normalize_ws",
    "similarity",
    "word_lcs_len",
]

import hashlib
from dataclasses import dataclass
from typing import Any


#: Prefix for in-band anchored keys (exact, survives a body rewrite).
ANCHOR_PREFIX = "a:"
#: Prefix for content-derived hash keys (survives reflow, changes on edit).
CONTENT_PREFIX = "c:"

#: The word-LCS ratio at and above which a positional pair reads as ``Edited``
#: (key inherited) rather than ``Inserted``. Mirrors the spec's "word-LCS ratio"
#: threshold.
EDIT_SIMILARITY_MIN = 0.5


def normalize_ws(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    Two blocks that differ only in whitespace (spaces, newlines, tabs) therefore
    hash to the same content key — they "survive a reflow."
    """
    return " ".join(text.split())


def content_hash(text: str) -> str:
    """The content-derived key body for ``text``.

    A SHA-256 (std-library, zero external deps) over the UTF-8 bytes of the
    whitespace-normalized text, truncated to 16 hex chars. Deterministic across
    processes and codecs; the prefix (``c:``) is added by :func:`block_key`.
    """
    return hashlib.sha256(normalize_ws(text).encode("utf-8")).hexdigest()[:16]


def block_key(block: dict[str, Any]) -> str:
    """The manufactured stable key for one block.

    ``a:<anchor>`` when the block carries an in-band ``anchor`` (exact,
    survives a full body rewrite); otherwise ``c:<content_hash(text)>>`` (survives
    reflow/reorder, changes on edit). The prefixes keep the anchored and content
    keyspaces disjoint.
    """
    anchor = block.get("anchor")
    if anchor is not None and anchor != "":
        return f"{ANCHOR_PREFIX}{anchor}"
    return f"{CONTENT_PREFIX}{content_hash(block['text'])}"


def _words(text: str) -> list[str]:
    return normalize_ws(text).split(" ") if text.strip() else []


def word_lcs_len(a: str, b: str) -> int:
    """The length of the longest common subsequence of the word lists of ``a``
    and ``b``.

    Words (not characters) are the alignment unit: a single-word edit in a
    sentence yields a high ratio, while a full rewrite yields ~0. Standard
    O(|a|·|b|) dynamic programming; the inputs are block-sized prose, not large
    documents.
    """
    wa = _words(a)
    wb = _words(b)
    if not wa or not wb:
        return 0
    # Rolling two-row DP keeps it O(min(|a|,|b|)) space.
    if len(wb) > len(wa):
        wa, wb = wb, wa
    prev = [0] * (len(wb) + 1)
    for x in wa:
        cur = [0] * (len(wb) + 1)
        xb = wb
        for j, y in enumerate(xb):
            cur[j + 1] = (prev[j] + 1) if x == y else max(cur[j], prev[j + 1])
        prev = cur
    return prev[len(wb)]


def similarity(a: str, b: str) -> float:
    """The word-LCS ratio between ``a`` and ``b``.

    ``2 · |lcs| / (|words(a)| + |words(b)|)`` — ``1.0`` when one is a word
    subsequence of the other, ``0.0`` when no word is shared. The Sorensen-Dice
    flavor of the overlap so a single-word edit in a long sentence stays near
    ``1.0`` and a true rewrite collapses toward ``0``.
    """
    wa = _words(a)
    wb = _words(b)
    denom = len(wa) + len(wb)
    if denom == 0:
        return 1.0
    return (2 * word_lcs_len(a, b)) / denom


@dataclass(frozen=True)
class Alignment:
    """The result of :func:`align` — keyed alignment of ``old`` onto ``new``.

    ``matches`` has one entry per ``new`` block, in ``new`` order:

    - ``"Same:<old_idx>"`` — the new block's key matched ``old[old_idx]``;
    - ``"Edited:<old_idx>"`` — keys differ but word-LCS similarity with
      ``old[old_idx]`` is ``>= EDIT_SIMILARITY_MIN`` (the key is inherited from
      the matched predecessor);
    - ``"Inserted"`` — no match (a genuine insert).

    ``removed`` lists the ``old`` indices that no ``new`` block matched, in
    ``old`` order.
    """

    matches: list[str]
    removed: list[int]


def align(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> Alignment:
    """Align ``old`` onto ``new`` by manufactured key, then word-LCS similarity.

    Two passes:

    1. **Key match (greedy, key-unique).** Each ``new`` block whose key an
       unmatched ``old`` block shares pairs with it (``Same:<old_idx>``). This is
       what makes a pure reorder read as all-``Same``.
    2. **Similarity match (positional).** The remaining unmatched ``new`` and
       ``old`` blocks are walked in order; a pair whose word-LCS similarity is
       ``>= EDIT_SIMILARITY_MIN`` reads as ``Edited:<old_idx>`` (key inherited),
       otherwise the ``new`` block is a genuine ``Inserted``.

    ``old`` blocks left unmatched after both passes are ``removed``.
    """
    old_keys = [block_key(b) for b in old]
    new_keys = [block_key(b) for b in new]
    matched_old: set[int] = set()
    matches: list[str] = [""] * len(new)
    old_by_key: dict[str, int] = {}

    # Pass 1: exact key match. A key maps to the first unmatched old index that
    # carries it (so duplicate keys do not collapse onto one old block).
    for ni, k in enumerate(new_keys):
        oi = old_by_key.get(k)
        if oi is None:
            for cand, ok in enumerate(old_keys):
                if cand in matched_old:
                    continue
                if ok == k:
                    oi = cand
                    old_by_key[k] = cand
                    break
        if oi is not None and oi not in matched_old:
            matched_old.add(oi)
            matches[ni] = f"Same:{oi}"

    # Pass 2: positional similarity for the unmatched remainder.
    unmatched_old = [i for i in range(len(old)) if i not in matched_old]
    uo_iter = iter(unmatched_old)
    for ni in range(len(new)):
        if matches[ni]:
            continue
        # Find the next unmatched old block by position; Edited if similar,
        # otherwise this new block is a genuine insert.
        oi = next(uo_iter, None)
        if (
            oi is not None
            and similarity(new[ni]["text"], old[oi]["text"]) >= EDIT_SIMILARITY_MIN
        ):
            matched_old.add(oi)
            matches[ni] = f"Edited:{oi}"
        else:
            # Not similar enough (or no old left): genuine insert. If we had
            # peeked an old index, put it back by advancing only on consume.
            if oi is not None:
                # Re-queue: rebuild the remaining iterator so the next new
                # block sees this old index again.
                remaining = [oi, *uo_iter]
                uo_iter = iter(remaining)
            matches[ni] = "Inserted"

    removed = [i for i in range(len(old)) if i not in matched_old]
    return Alignment(matches=matches, removed=removed)


def assign_stable_keys(
    old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[str]:
    """The manufactured key for each ``new`` block, flowing identity through edit.

    A ``Same`` new block keeps its matched ``old`` block's key verbatim; an
    ``Edited`` new block **inherits** its matched predecessor's key (so a small
    edit does not mint a fresh identity and trigger a remove + insert); an
    ``Inserted`` block mints a fresh key from its own text/anchor. This is the
    bridge from alignment to keyed reconciliation: the returned key list is a
    drop-in ``order`` for :func:`lazily.reconciliation.reconcile_ops`.
    """
    alignment = align(old, new)
    out: list[str] = []
    for ni, m in enumerate(alignment.matches):
        if m == "Inserted":
            out.append(block_key(new[ni]))
            continue
        # Same:<oi> or Edited:<oi> — inherit the matched old block's key.
        oi = int(m.split(":", 1)[1])
        out.append(block_key(old[oi]))
    return out
