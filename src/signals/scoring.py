"""
Scoring /100 (§6) — fonction pure : mêmes features ⇒ même score.

50 (base, portes passées) + 10 (retest DANS zone OB/FVG) + 10 (confirmation M5/M30)
+ 10 (contexte synthétique aligné) + 10 (régime de volatilité favorable)
+ 5 (force bougie signal) + 5 (RSI avec marge).
Grades : B 65-74 · A 75-84 · A+ 85-100. Seuil d'émission : 65.
"""
from __future__ import annotations

from typing import Tuple

THRESHOLD = 65


def grade_of(score: int) -> str:
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B"
    return "—"  # sous le seuil : pas de grade


def compute_score(
    retest_in_zone: bool,
    confirmation: bool,
    context_aligned: bool,
    regime_ok: bool,
    strength_ok: bool,
    rsi_ok: bool,
) -> Tuple[int, str, dict]:
    """Retourne (score, grade, détail des points par poste)."""
    breakdown = {
        "base": 50,
        "zone": 10 if retest_in_zone else 0,
        "confirmation": 10 if confirmation else 0,
        "contexte": 10 if context_aligned else 0,
        "régime": 10 if regime_ok else 0,
        "force": 5 if strength_ok else 0,
        "rsi": 5 if rsi_ok else 0,
    }
    score = sum(breakdown.values())
    return score, grade_of(score), breakdown
