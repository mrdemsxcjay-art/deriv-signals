# 🤖 Robot de Signaux — Indices Synthétiques Deriv

**Jump 10 (`JD10`) · Boom 1000 (`BOOM1000`) — 100 % gratuit, analyse uniquement (V10 supprimé le 07/09/2026).**

Le robot **n'exécute AUCUN ordre** : il analyse et envoie des signaux Telegram.
Aucune carte bancaire, aucune clé payante. Données : WebSocket Deriv officielle (app_id public gratuit).

> Règle n°1 : toute demande hors JD10 / BOOM1000 reçoit :
> « Je suis configuré uniquement pour JD10 / BOOM1000 pour maximiser la précision. »

---

## 🚀 Démarrage rapide (Étape 1)

```bash
cd deriv-signals
pip install -r requirements.txt

# Cycle unique : télécharge + valide D1/H4/H1/M30/M15/M5 × 3 instruments
python -m src.main --once

# Version rapide (100 bougies/TF, pour CI)
python -m src.main --once --fast

# Cycle d'analyse complet : stratégie 5 portes + scoring + tracker (Étape 3+)
python -m src.main --cycle

# Tests par module (données, analyse SMC, contexte synthétique, signaux)
python scripts/test_data.py && python scripts/test_analysis.py \
  && python scripts/test_synth.py && python scripts/test_signals.py

# Suite globale (toutes les suites test_*)
python scripts/run_all_tests.py
```

## 📁 Arborescence

```
deriv-signals/
├── config/settings.yaml          # instruments, seuils, planchers, stake
├── src/
│   ├── data/deriv_provider.py    # ✅ ÉTAPE 1 — WebSocket Deriv + cache + reconnexion (+ticks+M1 ét.2)
│   ├── analysis/                 # ✅ ÉTAPE 2 — indicateurs, structure SMC, OB, FVG, liquidité
│   ├── synthetics/context.py     # ✅ ÉTAPE 2 — spikes, jumps, régime de volatilité
│   ├── agents/strategy_agent.py  # ✅ ÉTAPE 3 — pipeline 5 portes + spécialisations
│   ├── signals/                  # ✅ ÉTAPE 3 — modèles, scoring, moteur, suivi TP/SL
│   ├── notifications/telegram.py # ⏳ ÉTAPE 5 — messages HTML
│   ├── storage/database.py       # ✅ ÉTAPE 3 — SQLite signaux + issues
│   └── main.py                   # ✅ boucle + mode --once
├── scripts/                      # ✅ test_data.py, run_all_tests.py
├── .github/workflows/            # ⏳ ÉTAPE 6 — moteur unique + page statut
└── dashboard/                    # ⏳ ÉTAPE 6 — page de statut statique
```

## 🗺️ Planning (une étape à la fois, validée avant la suivante)

| Étape | Contenu | État |
|---|---|---|
| 1 | Architecture + connexion Deriv + validation 6 TF × 3 instruments | ✅ validée |
| 2 | Indicateurs + SMC (BOS/CHoCH/OB/FVG) + contexte synthétique (spikes/jumps) | ✅ en validation |
| — | ⚠️ Corrections de spec mesurées (voir § Calibration) : JD10 ≈ 3 jumps/heure (pas /3 h), détection jumps au tick, spike 3× (pas 4×), dérive BOOM = médiane H1 | 📋 à valider |
| 3 | Pipeline 5 portes + spécialisations JD10/BOOM + scoring + SQLite | ✅ en validation |
| 4 | Calibration replay 21 j + validation 21 j hors-échantillon (C3 : +13,0R calib / +6,9R valid ; V10 0/243 TP → ⏸️ pause actée puis V10 supprimé le 07/09/2026) | ✅ validée |
| 5 | Messages Telegram HTML + message TEST | ✅ validée |
| 6 | Moteur GitHub Actions unique + cache base + page statut publique | ✅ validée |

## 📏 Calibration Étape 2 (mesures réelles du 06/09/2026 — priment sur le spec initial)

| Seuil | Spec initial | Mesure | Décision |
|---|---|---|---|
| Fréquence jumps JD10 | ~1 / 3 h (§1) | **2,76/h** (ticks, 8,3 h) ≈ 3/h officiels Deriv | ✅ corrigé : ~3/heure (~1/20 min) |
| Détection jump | gap M5 > seuil (§5) | 5 gaps en 17 j (les jumps tombent intra-bougie) | ✅ ticks : \|Δtick\| > **10 pts** (séparation nette 7,2 vs 11,2+) |
| Tailles jumps (n=23) | — | méd **43,8** · P90 **86,9** · max **132,6** · UP/DN 13/10 | 📋 règle SL JD10 à redessiner étape 3 (1,5×méd ≈ 66 > plancher 40) |
| Risque JUMP (>4 h FAIBLE / <1 h ÉLEVÉ, §4) | timing | intervalle médian 15 min ⇒ toujours « <1 h » | ⚠️ inapplicable → risque repensé étape 3 (SL vs distribution des jumps) |
| Spike BOOM (mult. médiane 100, M5) | 4× (§5) | 3× ⇒ **36 min** ≈ cible 30-35 ; 4× ⇒ 48 min (rate 1/3) | ✅ **3,0** (ampl. méd 20, P90 37, max 56) |
| Dérive BOOM/h | « moyenne » (§5) | moyenne : signe instable (+2,4/−1,2/−0,8/−4,0/+1,7) ; médiane H1 : toujours < 0 | ✅ **médiane H1** (≈ −6 pts/h) |
| V10 (sanity) | — | ATR M15 ≈ 3,9 · régime normal (pct 42) · vitesse ≈ 0,23 pts/min | ✅ définitions validées |

## 📏 Calibration Étape 4 (replay causal 21 j + validation 21 j, 06/09/2026)

Harness : pas 15 min sur bougies clôturées, fenêtres calibration [21→0 j] / validation [31→10 j], caches gelés (`data/backtest/histories_*{21,32}d.json`). `--set` strict (clé inconnue = `KeyError`), `--histories` pour rejouer un cache profond, `--end-offset-days` pour la validation hors-échantillon.

| Paramètre | Spec | Calibré | Preuve |
|---|---|---|---|
| SL JD10 (× jump médian) | 1,5 | **4,0** | grille monotone +1→+25R (1.5→5.0), plateau 4.0=5.0 ; valid +12R |
| Cassure M15 max (bougies) | 18 | **4** | global : total −24,3→−11,5R ; synergie BOOM en combo (+12R) |
| Plancher SL BOOM | 50 | **25** | grille 60→20 : −25/−15/−8/−4/**±0**/−16 (optimum intérieur, falaise à 20) |
| Âge max spike (BUY BOOM) | 45 min | **20 min** | combo (25,20) optimal |
| Seuil score | 65 | **65** (inchangé) | seuils 70/75/80 : R +12,6/+2,8/−3,5 — le score ne discrimine pas |
| TP ratio | 3,0 | 3,0 (constante spec, codée en dur dans `tracker.py`) | non réglable sans refonte spec |

Résultats (R / 21 j) — calibration : base −24,3 (175 signaux, 8,3/j) → **C3 +13,0** (152, 7,2/j). Validation : base −4,0 → **C3 +6,9** (103, 4,9/j).
**V10 : 0 TP sur 243 signaux** (calib + valid, marché plat −0,45 % ET tendanciel −2,19 %) → ⏸️ **pause proposée puis suppression définitive le 07/09/2026**. Sans V10 : calib **+23R** (4,4/j) ; valid **+9R** (**2,3/j** ✓ cible 2-3/j).
Pistes rejetées par mesure : seuil 50 (≈ base), SL V10 resserré (pire : −19 à −24R), fraîcheur H4, scores 85+ (toxiques : bonus « retest DANS zone » +10 contre-productif — retests à dist 0 : −5,4/−3,0/−13,0R).

## 📨 Notifications Telegram (étapes 5+)

- **Entrée** : `🔴 VENTE · BOOM1000` — prix (Entrée/Stop/Objectif), ratio 1:3, risque, score + grade, confluences ✅, CONTEXTE SYNTHÉTIQUE obligatoire, disclaimer.
- **Clôture** : `✅ OBJECTIF ATTEINT` / `🛑 STOP TOUCHÉ` / `⌛ EXPIRE SANS DÉCISION` — entrée → sortie, résultat en R et points, tenue, **R cumulé** + TP/SL + winrate.
- Logs moteur : `📩` = entrée notifiée, `📪` = clôture notifiée. Envois jamais bloquants (erreurs loggées, exit 1).
- Aperçu des rendus : `docs/message_preview.html`.

## 🚀 Déploiement (Étape 6 — 100 % gratuit)

**Repo PUBLIC recommandé** : minutes GitHub Actions illimitées + page statut publique.
Repo privé = 2000 min/mois (insuffisant : ~96 runs/jour).

1. Crée le repo GitHub `deriv-signals` (**Public**) — sans README/license (déjà ici).
2. `git remote add origin git@github.com:TON_USER/deriv-signals.git && git push -u origin main`
3. Secrets : Settings → Secrets → Actions → `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.
4. Page statut : Settings → Pages → Deploy from branch → `main` → `/docs` → `https://TON_USER.github.io/deriv-signals/`
5. Test manuel : onglet Actions → `Signaux Deriv` → `Run workflow` (job `live`).
6. Le moteur tourne ensuite **toutes les 15 min** (synthétiques 24/7, pas de pause week-end).

Fonctionnement : job `live` → `live_once.py` (analyse + Telegram) → `make_status.py` (page statut) → commit `data/signals.db` (état cooldown/quota/tracker) + `docs/index.html`. Job `tests` sur chaque push/PR. `data/cache` (bougies Deriv) via `actions/cache` — perte sans gravité (re-téléchargé).

## 🔧 Choix techniques (Étape 1)

- **`websocket-client` > `deriv-api`** : ~40 lignes, synchrone, robuste en CI ; le package officiel est async et fragile dans GitHub Actions — inutile ici (lecture seule, pas de trading).
- **Une seule connexion réutilisée** + backoff exponentiel (1s→16s), throttle 0,35s entre requêtes.
- **Cache incrémental** `data/cache/{symbole}_{granularité}.json` : seule la différence est téléchargée.
- **Anti-repaint** : la bougie en formation (`epoch + granularité > now`) est systématiquement exclue.
