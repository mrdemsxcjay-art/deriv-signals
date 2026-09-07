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

## H-SL-ADAPTIVE — VERDICTS OOS (2026-09-07, R corrigés, commit a7ea31d)

Sélection TRAIN→VAL→OOS exécutée (§14-18). Règle pré-enregistrée appliquée mécaniquement.
Fenêtres : JD10 bear TRAIN o21 (n=23) / VAL o14 (n=13) / OOS o0 (n=37).
BOOM bull TRAIN o7+o14 poolé dédupliqué (n=31, o14⊂o7 → PAS de VAL indépendante, limitation notée) / OOS o0 (n=47).
BOOM bear (n=4,0,0,0) et JD10 bull (n=0 partout) : INSUFFISANT, aucun verdict.

### Table verdict (§10) — paires (modèle, TP) sélectionnées sur TRAIN

| Famille | JD10 bear | BOOM bull |
|---|---|---|
| Fixe (référence) | réf. OOS +0.297 R+11 DD9 | réf. OOS +0.106 R+5 DD12 |
| ATR (B-atr15) | REJETÉE k1@4 : OOS +0.216 < FIXED, decay TRAIN −71%, sensibilité piquée ×2 | REJETÉE k1.2@3 : flip voisin k1.5 (+0.29→−0.226 TRAIN). RÉSERVE : OOS +0.277 remarquablement stable, DD5 |
| Structure (A2-ob) | **ACCEPTÉE b0@TP4** (conditionnelle*) : TRAIN +0.61/R+14, VAL +1.31/R+17, OOS +0.394/R+14.57 WR21.6% DD10.46 PF1.58 ; bat FIXED-best-TP sur les 3 folds ; domine FIXED à TOUS les TP sur OOS (6/6) ; sensibilité plate ; maxLS 5/5/7 ≤ FIXED 5/5/9 | REJETÉE b0.25@1.5 : flip voisins (−0.032/+0.048) |
| Volatilité (C) | REJETÉE C-jmp-P75@3 : TRAIN +0.217 → OOS −0.027 (échec OOS) | **ACCEPTÉE C-spk-P50@TP3** (conditionnelle*) : TRAIN +0.2/R+6, OOS +0.304/R+14 WR32.6% DD5 PF1.45 ; DD ÷2.4 vs FIXED ; maxLS 5/5 vs 9/9 ; edge = régime bull neutralisé (0.0 vs −9.0 FIXED sur les 2 folds, +9R exactement l'écart total) ; jamais pire que FIXED dans aucun régime×fold |
| Hybride (D1-max / EXT-k1.5) | REJETÉES : D1 OOS +0.124 < FIXED + gain D2 +0.01 négligeable, rails 30-82% (mécanisme = rails) ; EXT OOS +8.6 < FIXED +11 | REJETÉES : D1/EXT négatifs OOS, rails 55-70% |

(*) ACCEPTÉE conditionnelle = OOS passé, activation INTERDITE avant : (1) validation explicite utilisateur du présent rapport,
(2) spec stake Deriv garantissant risque 1$ invariant (C-P50@3 : ratio 3 inchangé ; A2@4 : chantier C12 tracker ratio variable R=+m + tests),
(3) portage sl_models→src + tests + parité replay, (4) règles NO_TRADE (OB/spikes insuffisants), (5) suivi paper proposé (BOOM : pas de VAL indépendante).

### 7 critères (§8) — les 2 ACCEPTÉES
A2 JD10 : (1) exp ≥ FIXED-best sur 3/3 folds ✓ (2) OOS +0.394/PF1.58, positif à tous TP ✓ (3) DD 10.46 ≤ 13.5 (1.5× FIXED 9) ✓
(4) causal/closed-only ✓ (5) OOS+TRAIN+VAL mono-régime bear_profond (73/73) → stabilité inter-régimes NON évaluable ; cohérence inter-fenêtres 3/3 ✓ + note
(6) R-comptable 1$ invariant ✓ (7) perte −1R plafonnée, WR 21.6% (profil 4R : pertes fréquentes, gains rares — signalé), pas d'achat de WR par largeur ✓.
C-P50 BOOM : (1) TRAIN +6 vs FIXED ≤+0.5, OOS +14 vs +5 ✓ (2) OOS +0.304/PF1.45 ✓ (3) DD 5 vs 12 ✓✓ (4) ✓
(5) ≥ FIXED dans chaque régime×fold (bull 0 vs −9 les 2 folds) ✓✓ (6) ✓ (7) SL 22.3 < 25 (RESSERRÉ, direction anti-élargissement) ✓.

### Faits notables versés au dossier
- Falaise TP BOOM bull ~75-90 pts : k1.2(TP74)→k1.5(TP93) et P60(TP75)→P70(TP87) s'effondrent ; C-P50 (TP67) en marge, k1.2 (TP74) au bord → appuie le REJET ATR + la réserve.
- A2 JD10 : optimum TRAIN au bord de grille (TP4, monotone) vs argmax OOS @TP3 (+20.6) → TP-instabilité notée, variante robuste (positive partout).
- max(structure,k×ATR) simple (préféré §19) battu par les formes pures : EXT OOS < FIXED des deux côtés, dominé par les rails.
- S2 live-3 (top-tick, tous modèles SL) : problème ENTRÉE, pas SL → soutient C3. Biais VALIDATION o14⊂o7 BOOM documenté (inclusion totale, 0 signal nouveau).
- Mémo C12 : tracker.py hardcode TP=+3R (production @3 non affectée ; tout TP≠3 exige R=+m + tests).

## PAPER H-SL-ADAPTIVE — lancé 2026-09-07 (décision utilisateur : PAPER D'ABORD)

Candidats EXPÉRIMENTAUX (pas des règles de production), suivis en miroir :
- JD10 bearish : A2-ob-b0 @ TP4 vs baseline fixe 40 pts (OOS réf exp +0.394, n=37).
- BOOM1000 bullish : C-spk-P50 @ TP3 vs baseline fixe 25 pts (OOS réf exp +0.304, n=47).
Réserves notées : JD10 (échantillon limité, mono-régime, TP4 exige C12) > BOOM (ratio 3
inchangé, DD ÷2.4, pas de changement tracker). C-P50 = candidat le plus rassurant.

Règles verrouillées : (1) live STRICTEMENT inchangé ; (2) 0 ordre réel supplémentaire ;
(3) miroir généré exactement au même moment que le signal live (même cycle, mêmes
bougies clôturées) ; (4) fiche complète (entrée, SL fixe/adaptatif, TP, résultats des
2 scénarios, MAE/MFE, durée, score, régime, timestamp, latence) ; (5) comparaison
systématique LIVE BASELINE vs PAPER ; (6) paramètres GELÉS (garde mécanique :
empreinte config vérifiée chaque cycle, dérive = paper désactivé + erreur visible) ;
(7) INTERDICTION de ré-optimiser sur les résultats paper ; (8) résultats conservés
même si sous-performance (base paper auto-suffisante, snapshots baseline inclus).

Durée : min 1-2 semaines + N signaux suffisant. Rapport final imposé (Instrument,
Modèle, Signaux, WR, Expectancy, R total, DD, MAE, MFE, Baseline, Verdict).
Critère : activation réelle UNIQUEMENT si le paper confirme les refs OOS, sinon
KEEP CURRENT LIVE. AUCUNE activation automatique.
