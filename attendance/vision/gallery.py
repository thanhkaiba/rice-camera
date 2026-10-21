"""In-memory face gallery: several reference embeddings per employee, matched by cosine similarity.

Optional centering (recognition.center_embeddings): face embeddings from one camera all lean
the same way (the camera, the light, "it is a face"). That shared direction says nothing
about who someone is, but it inflates every score and makes "hub" people who look similar
to everybody. Removing it, estimated as the mean of each person's mean so people with many
photos do not dominate, cut wrong identifications from 20 frames to 4 in another team's
measurements on their camera. Scores are lower afterwards, so accept_score must be tuned
again: `evaluate` compares both ways on your photos.
"""
from __future__ import annotations

import threading

import numpy as np

# With fewer people the shared direction cannot be told apart from their identities.
MIN_PEOPLE_TO_CENTER = 8


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def shared_direction(by_person: dict[str, np.ndarray]) -> np.ndarray | None:
    """Unit vector every person's embeddings share, or None with too few people."""
    means = [normalize(refs.mean(axis=0)) for refs in by_person.values() if len(refs)]
    if len(means) < MIN_PEOPLE_TO_CENTER:
        return None
    return normalize(np.mean(np.stack(means), axis=0))


def project(matrix: np.ndarray, direction: np.ndarray | None) -> np.ndarray:
    """Remove `direction` from each row (or a single vector) and normalise again."""
    m = np.asarray(matrix, dtype=np.float32)
    if direction is None:
        return m
    single = m.ndim == 1
    m = np.atleast_2d(m)
    m = m - np.outer(m @ direction, direction)
    m = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
    return m[0] if single else m


class Gallery:
    def __init__(self, items: list[tuple[str, np.ndarray]] | None = None, center: bool = False):
        self._lock = threading.Lock()
        self.center = center
        self._raw: dict[str, np.ndarray] = {}
        self._by_person: dict[str, np.ndarray] = {}  # as compared: centered when centering is on
        self._direction: np.ndarray | None = None
        self.replace(items or [])

    @classmethod
    def from_normalized(cls, grouped: dict[str, np.ndarray], center: bool = False) -> "Gallery":
        """Reuse prepared reference matrices without normalizing and stacking every row again.
        The matrices are treated as read-only; the mapping is copied before gallery updates.
        """
        gallery = cls(center=center)
        gallery._raw = dict(grouped)
        gallery._refresh()
        return gallery

    def _refresh(self) -> None:
        """Recompute the shared direction and the compared references (call with the lock held)."""
        self._direction = shared_direction(self._raw) if self.center else None
        self._by_person = {p: project(m, self._direction) for p, m in self._raw.items()}

    def replace(self, items: list[tuple[str, np.ndarray]]) -> None:
        grouped: dict[str, list[np.ndarray]] = {}
        for person, vec in items:
            grouped.setdefault(person, []).append(normalize(vec))
        with self._lock:
            self._raw = {p: np.stack(v) for p, v in grouped.items()}
            self._refresh()

    def add(self, person: str, vec: np.ndarray) -> None:
        vec = normalize(vec)[None, :]
        with self._lock:
            current = self._raw.get(person)
            self._raw[person] = vec if current is None else np.vstack([current, vec])
            self._refresh()

    @property
    def centered(self) -> bool:
        return self._direction is not None

    def prepare(self, embedding: np.ndarray) -> np.ndarray:
        """An embedding as the gallery compares it (normalised, centered when that is on)."""
        return project(normalize(embedding), self._direction)

    def snapshot(self) -> dict[str, np.ndarray]:
        """person -> matrix of reference embeddings as compared (see prepare)."""
        with self._lock:
            return dict(self._by_person)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {p: len(m) for p, m in self._by_person.items()}

    def match(self, embedding: np.ndarray, k: int = 2) -> list[tuple[str, float]]:
        """Return the k best (person, score) pairs, best first.

        A person's score is the mean of their two closest reference images, which is less
        sensitive to a single odd reference photo than taking only the best one.
        """
        with self._lock:
            items = list(self._by_person.items())
            emb = project(normalize(embedding), self._direction)
        scores = []
        for person, refs in items:
            sims = refs @ emb
            top = np.sort(sims)[-2:] if len(sims) >= 2 else sims
            scores.append((person, float(top.mean())))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]
