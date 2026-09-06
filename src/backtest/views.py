"""
Vues pré-calculées du replay : détecteurs UNE fois sur l'historique complet,
puis filtrés par curseur — IDENTIQUE au calcul frais (causalité prouvée ét. 2).

- Curseur `n` sur un TF = nombre de barres visibles (préfixe [:n]).
- Visibilité : structure/OB si break_index ≤ n-1 ; FVG si index ≤ n-2
  (la FVG centrée en i exige la barre i+1) ; spike si index ≤ n-1.
- Mitigation au curseur : mitigé ⟺ mitigated_index ≤ n-1 (copies, jamais muté).
Fidélité prouvée bit-à-bit dans test_replay.py (frais vs pré-calculé).
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Any, Dict, List

from ..analysis.fvg import detect_fvg
from ..analysis.order_blocks import detect_order_blocks
from ..analysis.structure import detect_structure
from ..synthetics.context import detect_spikes


def build_symbol_views(m15: List[dict], h4: List[dict], d1: List[dict], m5: List[dict],
                       strength: int = 2, spike_window: int = 100,
                       spike_mult: float = 3.0) -> Dict[str, Any]:
    s15 = detect_structure(m15, strength)
    s_h4 = detect_structure(h4, strength)
    return {
        "M15": {"struct": s15, "obs": detect_order_blocks(m15, s15),
                "fvgs": detect_fvg(m15), "epochs": [c["epoch"] for c in m15]},
        "H4": {"struct": s_h4, "obs": detect_order_blocks(h4, s_h4),
               "fvgs": detect_fvg(h4), "epochs": [c["epoch"] for c in h4]},
        "D1": {"struct": detect_structure(d1, strength),
               "epochs": [c["epoch"] for c in d1]},
        "spikes": detect_spikes(m5, spike_window, spike_mult),
    }


def _unmitigated_after(items: List[dict], n: int) -> List[dict]:
    """Copies avec mitigation ajustée au curseur n (barres visibles [:n])."""
    out = []
    for z in items:
        if z.get("mitigated") and z.get("mitigated_index") is not None \
                and z["mitigated_index"] > n - 1:
            z = dict(z)
            z["mitigated"] = False
            z["mitigated_index"] = None
            z["mitigated_epoch"] = None
        out.append(z)
    return out


def filter_prep(view_tf: Dict[str, Any], n: int, with_zones: bool) -> Dict[str, Any]:
    """Prep `{struct, obs, fvgs}` filtrée au curseur n (cf. règles de visibilité)."""
    prep: Dict[str, Any] = {
        "struct": [e for e in view_tf["struct"] if e["index"] <= n - 1]}
    if with_zones:
        obs = [o for o in view_tf["obs"] if o["break_index"] <= n - 1]
        fvgs = [f for f in view_tf["fvgs"] if f["index"] <= n - 2]
        prep["obs"] = _unmitigated_after(obs, n)
        prep["fvgs"] = _unmitigated_after(fvgs, n)
    return prep


def filter_spikes(spikes: List[dict], n: int) -> List[dict]:
    return [s for s in spikes if s["index"] <= n - 1]


def count_closed(epochs: List[int], granularity: int, T: int) -> int:
    """Nombre de barres clôturées à l'instant T (epoch + gran ≤ T)."""
    return bisect_right(epochs, T - granularity)
