# AUDIT — Distribution des signaux par instrument (07/09/2026)

Demande : Telegram reçoit presque exclusivement des signaux BOOM1000. Pourquoi ?
Méthode : lecture seule, 0 modification. Funnel rejoué avec le CODE DE PRODUCTION
et les SETTINGS LIVE (`scripts/audit_funnel.py`, 100 cycles M15 ≈ 25 h, fenêtre live
+ 5 fenêtres de contrôle sur 18 jours). Preuve live : cycle réel en dry-run.

## Contexte live (faits)
- Live = code `01f042c` (C1/paper NON déployés : pre-C1, sans garde fraîcheur).
- Historique live : ~10 h, 3 commits, **1 seul signal total** (BOOM SELL 06/09 21:23 → SL).
- Stratégie live == locale (diff vide) ; settings utiles identiques.

## Matrice (100 cycles, fenêtre live)
| Instrument | Direction | Data OK | D1 | H4 | H1 | M15/Spike | Score~ | Seuil 65+ | Filtre spk/jmp | SL valide | Latence | Émis (sim) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BOOM1000 | BUY | 100 | — | — | — | spike 36 → M15 6 | 85/85 | 6 | 64 bloqués | toujours | N/A (pré-C1) | 1 |
| BOOM1000 | SELL | 100 | 100 | 79 | 75 | M15 25 | 78/80 | 24 | non bloquant | toujours | N/A | 1 |
| JD10 | BUY | 100 | 0 | 0 | 0 | 0 | — | 0 | non bloquant | toujours | N/A | 0 |
| JD10 | SELL | 100 | 100 | 53 | 33 | M15 0 | — | 0 | non bloquant | toujours | N/A | 0 |

## Rejets (fenêtre live)
- BOOM BUY : spike 64, structure 28, cooldown 5, retest 2.
- BOOM SELL : structure 75, cooldown 23, score 1.
- JD10 BUY : structure 100 (D1 EMA : clôture ≤ EMA200, 100 %).
- JD10 SELL : structure 100 (H4 EMA50 : 47, H1 EMA50 : 20, M15 : 20 trop ancienne + 13 mauvaise direction).

## 18 jours (600 cycles) : BOOM bull 63→17émis, BOOM bear 43→3, JD10 bull 0→0, JD10 bear 42→6.
JD10 bull : D1=0/600 (régime D1 baissier 18 j+). JD10 bear : tout ou rien selon
l'alignement H4/H1/M15 (28/08 : 33 setups ; 4 jours sur 6 : 0). Scoring : JD10 NON
pénalisé (méd 80, contexte 10/10, régime 9.5 ; 0 « score insuffisant » en 600 cycles).

## Réponses (8 points)
1. **Cause principale** : régime D1 baissier + cascade EMA/structure : JD10-bull
   D1-verrouillé, JD10-bear décimé H4→H1→M15. Fenêtre live : 0 setup JD10 complet.
2. **Secondaires** : (a) BIAS VOLONTAIRE documenté — BOOM BUY contourne D1/H4/H1
   (BOOM émet dans tous les régimes) ; (b) M15 âge≤4 (étape 4) sévère mais neutre ;
   (c) cooldown 180 min mange les grappes (36/42 JD10 sim, idem BOOM).
3. **JD10 auraient dû être générés (fenêtre live)** : 0 — le moteur n'a rien manqué.
4. **Étape d'élimination** : D1 (bull), H4-EMA50 puis H1-EMA50 puis M15 (bear).
5. **Bug** : NON. Données OK (symboles valides, clôtures seules),ticks JD10 OK,
   Telegram agnostique (0 JD10 généré → rien perdu en aval).
6. **Biais volontaire** : OUI, un seul — l'exception BOOM BUY (documentée).
7. **Normal / anormal** : NORMAL (conforme aux règles), asymétrie assumée + régime
   défavorable prolongé expliquent 100 % du ratio perçu.
8. **Correction minimale (SANS activation)** : aucune (pas de bug). Options à valider :
   ne rien faire (le régime changera — preuve 28/08) ; ou re-discuter M15-âge / symétrie
   BOOM BUY via replay dédié. Vigilance : au push C1, le garde « ticks frais » ne doit
   pas fraiser JD10 à froid (cache CI à vérifier).
