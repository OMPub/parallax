"""Novelty filtering — the rigorous half of "don't walk the same path twice".

Embedding cosine when a local embedder is reachable (catches same-vuln
paraphrases that lexical matching misses); character-shingle Jaccard otherwise.
``Checker`` exposes one interface so the survey loop is agnostic to which is live.
"""

import re

_WORD = re.compile(r"[a-z0-9]+")


def _shingles(text, k=4):
    norm = " ".join(_WORD.findall(text.lower()))
    if len(norm) < k:
        return {norm} if norm else set()
    return {norm[i:i + k] for i in range(len(norm) - k + 1)}


def similarity(a, b):
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def max_similarity(claim, prior):
    best = 0.0
    for p in prior:
        s = similarity(claim, p)
        if s > best:
            best = s
    return best


def is_novel(claim, prior, threshold=0.6):
    return max_similarity(claim, prior) < threshold


def cosine(a, b):
    dot = sa = sb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        sa += x * x
        sb += y * y
    if sa == 0 or sb == 0:
        return 0.0
    return dot / ((sa ** 0.5) * (sb ** 0.5))


class Checker:
    def __init__(self, prior_texts, prior_vecs=None, jaccard_threshold=0.6, cos_threshold=0.72):
        self.texts = list(prior_texts)
        self.vecs = list(prior_vecs) if prior_vecs is not None else None
        self.jt = jaccard_threshold
        self.ct = cos_threshold

    @property
    def mode(self):
        return "embeddings" if self.vecs is not None else "jaccard"

    def is_novel(self, text, vec=None):
        if self.vecs is not None and vec is not None:
            return all(cosine(vec, pv) < self.ct for pv in self.vecs)
        return is_novel(text, self.texts, self.jt)

    def add(self, text, vec=None):
        self.texts.append(text)
        if self.vecs is not None and vec is not None:
            self.vecs.append(vec)
