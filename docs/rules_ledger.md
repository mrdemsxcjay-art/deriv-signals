# Registre des règles — anti data-mining (§14)

Statuts : `OBSERVATION` → `HYPOTHÈSE` → `TEST` → `VALIDATION` → `OUT-OF-SAMPLE` → `ACCEPTÉE`/`REJETÉE`.
Interdiction de passer directement d'observation à règle. Une règle ACCEPTÉE n'est
activée en production qu'après validation utilisateur explicite du rapport comparatif.

## Correctness / infrastructure (pas des règles stratégiques)

| ID | Objet | Statut | Preuve |
|---|---|---|---|
| C1 | Idempotence (IDs déterministes, flags notified, commit always, NO_TRADE périmé, SL/TP-déjà-touché) | IMPLÉMENTÉ + TESTÉ (8/8, 11/11, replay inchangé) | commit `c2cfd90`, `scripts/test_idempotency.py` |
| C11 | Winrate source unique (`get_stats` partout) | OBSERVATION (latent, EXPIRE=0) | audit §35 |

## Stratégie — en cours d'instruction (AUCUNE activée)

| ID | Hypothèse | Statut | Protocole |
|---|---|---|---|
| C2 | Calibration SELL multi-régime (fenêtres × offsets) | HYPOTHÈSE | TRAIN/VAL/OOS (JD10 : o21/o14/o0 ; BOOM : o7+o14/o0, cf. mur M5) |
| C3 | Anti-top-spike buys (E : interdiction barre-spike) | HYPOTHÈSE | variantes A–E §7 + §8, sélection OOS |
| C4 | Anti-overdue sells (spike imminent) | HYPOTHÈSE | comptage SL-sur-spike + gate, sélection OOS |
| C5 | SL JD10 robuste (médiane élargie + floor/cap) | HYPOTHÈSE | analyse MAE/MFE + variantes SL §9 |
| C7 | Score : ablation A–F + seuils 55–80 | HYPOTHÈSE | ablation §5 + seuils §6, TRAIN→VAL→OOS |
| C8 | Latence : edge réel vs exécution retardée | HYPOTHÈSE | variantes A–F §10 (niveaux fixes ET distances fixes) |
| C10 | H4 + proximité OB/FVG (distances × ATR) | HYPOTHÈSE | grille distances §13, sélection OOS |
| C13 | Filtre liquidity sweep | HYPOTHÈSE (biais : SUPPRIMER si OOS ≤ 0) | filtre vs aucun §12 |
| H-SL-ADAPTIVE | SL adaptatif par signal : SL = f(structure, ATR, volatilité, régime, MAE) déterministe ; modèles A (structurel) / B (ATR) / C (spike-P) / D (hybride) × TP {1.5–4R} ; risque $ constant ; sélection TRAIN→VAL→OOS | HYPOTHÈSE | spec 07/09/2026 : MAE/MFE obligatoires, 7 critères décision, REJECT si aucun modèle qualifié ; EXT simple `max(structure, k×ATR)` si contexte-dépendance |

## Découvertes C1 (nouvelles, à instruire)

| ID | Observation | Statut |
|---|---|---|
| C16 | `save_signal` en OR REPLACE ressuscitait les CLOSED (masqué par IDs wall-clock) | CORRIGÉ (OR IGNORE) + test 2/11 |
| C17 | Signal opposé pendant position ouverte : cooldown seul garde-fou (180 min), pas de garde directionnelle | OBSERVATION (à mesurer : fréquence en replay) |
| C18 | Page statut : winrate TP/(TP+SL) ≠ Telegram TP/résolus (diverge au 1er EXPIRE) | OBSERVATION (= C11, à fusionner) |

## Protocole folds (décidé 07/09/2026, plafonds Deriv mesurés : M5 17,4 j / M15 52 j)

- JD10 : TRAIN = offset 21 j → VAL = offset 14 j → OOS = offset 0 (21 j, disjointes).
- BOOM : TRAIN = offsets 7+14 j poolés (13,8 j effectifs M5, chevauchants — déclaré) → OOS = offset 0.
  Pas de VAL indépendant possible pour BOOM (mur M5) → robustesse par sensibilité ±1 cran.
- 42/60/90 j : INFASABLE sans changer la stratégie (M15 : 42×96+2880 > 4999).
- Recommandation : accumulation forward (snapshots M5/M15 mensuels) → 90 j dans ~10 semaines.
