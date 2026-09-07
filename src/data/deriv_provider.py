"""
DerivProvider — Source de données UNIQUE du robot (Étape 1).

- WebSocket Deriv officielle, app_id public gratuit (aucun token : données de marché uniquement).
- UNE SEULE connexion réutilisée + reconnexion automatique avec backoff exponentiel.
- Cache incrémental local (ne re-télécharge que les bougies manquantes).
- Anti-repaint : seules les bougies CLÔTURÉES sont retournées (la bougie en formation est exclue).
- Throttle entre requêtes (respect des limites Deriv).

Choix technique : `websocket-client` (~40 lignes de WebSocket, robuste en CI, zéro
dépendance lourde) plutôt que le package `deriv-api` (async, plus fragile dans
GitHub Actions et inutile ici : on ne fait que lire des bougies, pas de trading).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import websocket  # websocket-client

log = logging.getLogger("deriv_provider")

# Granularités Deriv (secondes) — les 6 timeframes requis du projet.
TIMEFRAMES: Dict[str, int] = {
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

# Symboles API Deriv — RÈGLE N°1 : rien d'autre ne doit être demandé.
ALLOWED_SYMBOLS = {"JD10", "BOOM1000"}

# Granularité auxiliaire (hors des 6 TF stratégiques) : M1 (60 s) réservé à la
# DÉTECTION des jumps JD10 — mesuré Étape 2 : le M5 est trop grossier, un jump
# intra-bougie s'y noie dans le range médian (44 pts). Le M1 n'est jamais
# utilisé pour la stratégie (pipeline 5 portes inchangé : D1→M5).
AUX_GRANULARITIES = {60}
ALLOWED_GRANULARITIES = set(TIMEFRAMES.values()) | AUX_GRANULARITIES


class DerivAPIError(Exception):
    """Erreur renvoyée par l'API Deriv (ex : symbole invalide)."""


class DerivProvider:
    """Fournit les bougies Deriv avec cache + reconnexion. Réutiliser UNE instance."""

    def __init__(
        self,
        ws_url: str = "wss://ws.derivws.com/websockets/v3?app_id=1089",
        cache_dir: str = "data/cache",
        timeout: int = 20,
        throttle_seconds: float = 0.35,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        use_cache: bool = True,
    ) -> None:
        self.ws_url = ws_url
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.throttle_seconds = throttle_seconds
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.use_cache = use_cache
        self._ws: Optional[websocket.WebSocket] = None
        self._last_call = 0.0
        self._pip_sizes: Dict[str, int] = {}
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---------------- connexion ----------------

    def _ensure_connection(self) -> None:
        """Ouvre (ou ré-ouvre) l'unique connexion WebSocket."""
        if self._ws is not None:
            try:
                # ping léger : si le socket est mort, l'exception déclenche une reconnexion
                self._ws.settimeout(2)
                self._ws.ping()
                self._ws.settimeout(self.timeout)
                return
            except Exception:
                self.close()
        self._ws = websocket.create_connection(self.ws_url, timeout=self.timeout)

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        finally:
            self._ws = None

    def _throttle(self) -> None:
        wait = self.throttle_seconds - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Envoie une requête JSON avec retries + backoff exponentiel."""
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                self._throttle()
                self._ensure_connection()
                assert self._ws is not None
                self._ws.send(json.dumps(payload))
                raw = self._ws.recv()
                data = json.loads(raw)
                if "error" in data:
                    raise DerivAPIError(
                        f"{data['error'].get('code', '?')}: {data['error'].get('message', '?')}"
                    )
                return data
            except DerivAPIError:
                raise  # erreur métier : inutile de réessayer (ex : symbole invalide)
            except Exception as exc:  # coupure réseau, timeout, socket mort...
                last_err = exc
                self.close()
                if attempt >= self.max_retries:
                    break
                delay = self.backoff_base * (2 ** attempt)
                log.warning(
                    "Deriv: tentative %d/%d échouée (%s) → reconnexion dans %.1fs",
                    attempt + 1, self.max_retries + 1, exc, delay,
                )
                time.sleep(delay)
        raise ConnectionError(
            f"Deriv injoignable après {self.max_retries + 1} tentatives : {last_err}"
        )

    # ---------------- bougies ----------------

    @staticmethod
    def _cache_path(cache_dir: str, symbol: str, granularity: int) -> str:
        return os.path.join(cache_dir, f"{symbol}_{granularity}.json")

    @staticmethod
    def _drop_forming_candle(candles: List[Dict[str, Any]], granularity: int) -> List[Dict[str, Any]]:
        """Anti-repaint : exclut la bougie en cours de formation (non clôturée)."""
        if not candles:
            return candles
        now = int(time.time())
        # epoch = open_time ; close_time = epoch + granularity (marge 5 s d'horloge)
        closed = [c for c in candles if c["epoch"] + granularity <= now - 5]
        return closed

    def get_candles(
        self,
        symbol: str,
        granularity: int,
        count: int = 1000,
        use_cache: Optional[bool] = None,
        end: Any = "latest",
    ) -> List[Dict[str, Any]]:
        """
        Retourne les `count` dernières bougies CLÔTURÉES (triées par epoch croissant).

        Cache incrémental : si le cache couvre déjà une partie, seule la
        différence est téléchargée puis fusionnée.
        Pagination historique (replay) : end=<epoch> retourne le batch brut
        se terminant à `end` (ni fusion, ni écriture cache).
        """
        if symbol not in ALLOWED_SYMBOLS:
            raise ValueError(
                f"Symbole '{symbol}' refusé — Je suis configuré uniquement pour "
                f"JD10 / BOOM1000 pour maximiser la précision."
            )
        if granularity not in ALLOWED_GRANULARITIES:
            raise ValueError(f"Granularité {granularity}s non supportée (cf. TIMEFRAMES).")

        do_cache = self.use_cache if use_cache is None else use_cache
        cache_path = self._cache_path(self.cache_dir, symbol, granularity)
        cached: List[Dict[str, Any]] = []
        if do_cache and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f).get("candles", [])
            except Exception as exc:
                log.warning("Cache illisible %s (%s) → ignoré", cache_path, exc)
                cached = []

        # --- combien de bougies manquent ? ---
        now = int(time.time())
        if cached and len(cached) >= count:
            # Cas nominal : le cache couvre déjà la profondeur → on ne
            # télécharge que les bougies apparues depuis (incrémental pur).
            last_epoch = cached[-1]["epoch"]
            missing = (now - last_epoch) // granularity + 3  # +3 : marge
            missing = max(5, min(missing, count + 1))
        else:
            # Cache vide ou trop peu profond : Deriv ne renvoyant que les
            # N dernières, il faut re-télécharger toute la fenêtre (+1 car
            # la bougie en formation sera exclue par l'anti-repaint).
            missing = count + 1

        if end != "latest":
            missing = count + 1  # pagination : on veut exactement le batch demandé
        # --- téléchargement (seulement le manque) ---
        payload = {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": int(missing),
            "end": end,
        }
        t0 = time.time()
        data = self._request(payload)
        fresh = data.get("candles", [])
        if "pip_size" in data:
            self._pip_sizes[symbol] = int(data["pip_size"])
        dt = time.time() - t0
        log.debug("%s %ss : %d bougies téléchargées en %.2fs", symbol, granularity, len(fresh), dt)

        if end != "latest":
            # Pagination historique (replay) : batch brut trié, ni fusion ni cache.
            out = sorted(fresh, key=lambda c: c["epoch"])
            return self._drop_forming_candle(out, granularity)

        # --- fusion cache + frais (dédupliquée par epoch, triée) ---
        by_epoch: Dict[int, Dict[str, Any]] = {c["epoch"]: c for c in cached}
        for c in fresh:
            by_epoch[c["epoch"]] = c
        merged = [by_epoch[e] for e in sorted(by_epoch)]
        merged = self._drop_forming_candle(merged, granularity)
        merged = merged[-count:]  # borne mémoire : on garde la fenêtre demandée

        if do_cache:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"symbol": symbol, "granularity": granularity,
                     "updated_at": now, "candles": merged},
                    f,
                )
            os.replace(tmp, cache_path)

        return merged

    def get_timeframe(self, symbol: str, timeframe: str, count: int) -> List[Dict[str, Any]]:
        """Raccourci : timeframe 'M5'...'D1' au lieu de la granularité en secondes."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Timeframe '{timeframe}' inconnu (attendu : {list(TIMEFRAMES)}).")
        return self.get_candles(symbol, TIMEFRAMES[timeframe], count)

    def get_all_timeframes(
        self, symbol: str, counts: Optional[Dict[str, int]] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Télécharge les 6 timeframes pour un instrument. Retourne {TF: bougies}."""
        counts = counts or {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        for tf, gran in TIMEFRAMES.items():
            out[tf] = self.get_candles(symbol, gran, counts.get(tf, 500))
        return out

    def pip_size(self, symbol: str) -> Optional[int]:
        return self._pip_sizes.get(symbol)

    # ---------------- ticks (détection des jumps JD10 — Étape 2) ----------------

    def get_ticks(
        self,
        symbol: str,
        count: int = 2000,
        end: Any = "latest",
        use_cache: Optional[bool] = None,
        max_cache: int = 40000,  # ≈ 11 h à ~1 tick/s ⇒ couvre les 20 derniers jumps
    ) -> List[Dict[str, Any]]:
        """Ticks [{"epoch", "price"}] triés par epoch croissant.

        - end="latest" : télécharge les `count` derniers ticks et les fusionne
          dans le cache roulant (1 seule requête) ; retourne le cache fusionné
          si use_cache, sinon le batch brut (déterministe, pour les tests).
        - end=<epoch> : pagination historique (calibration) ; le batch est
          aussi fusionné au cache (utile tant que le cache n'est pas plein).
        """
        if symbol not in ALLOWED_SYMBOLS:
            raise ValueError(
                f"Symbole '{symbol}' refusé — Je suis configuré uniquement pour "
                f"JD10 / BOOM1000 pour maximiser la précision."
            )
        do_cache = self.use_cache if use_cache is None else use_cache
        cache_path = os.path.join(self.cache_dir, f"{symbol}_ticks.json")
        cached: List[Dict[str, Any]] = []
        if do_cache and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f).get("ticks", [])
            except Exception as exc:
                log.warning("Cache ticks illisible %s (%s) → ignoré", cache_path, exc)

        payload: Dict[str, Any] = {"ticks_history": symbol, "style": "ticks",
                                   "count": int(count), "end": end}
        data = self._request(payload)
        hist = data.get("history", {})
        batch = [{"epoch": int(t), "price": float(p)}
                 for t, p in zip(hist.get("times", []), hist.get("prices", []))]
        batch.sort(key=lambda t: t["epoch"])

        if not do_cache:
            return batch
        by_epoch: Dict[int, Dict[str, Any]] = {t["epoch"]: t for t in cached}
        for t in batch:
            by_epoch[t["epoch"]] = t  # même seconde ⇒ dernier tick gardé
        merged = [by_epoch[e] for e in sorted(by_epoch)][-max_cache:]
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"symbol": symbol, "updated_at": int(time.time()),
                       "ticks": merged}, f)
        os.replace(tmp, cache_path)
        return merged

    # ---------------- validation qualité ----------------

    @staticmethod
    def validate(
        candles: List[Dict[str, Any]], granularity: int
    ) -> Dict[str, Any]:
        """
        Contrôle qualité d'une série : tri, trous, doublons, cohérence OHLC, fraîcheur.
        Retourne un rapport dict (jamais d'exception sur données incomplètes).
        """
        report: Dict[str, Any] = {
            "count": len(candles),
            "ordered": True,
            "gaps": 0,
            "duplicates": 0,
            "ohlc_errors": 0,
            "first_epoch": None,
            "last_epoch": None,
            "last_close_utc": None,
            "age_seconds": None,
            "ok": False,
        }
        if not candles:
            return report
        epochs = [c["epoch"] for c in candles]
        report["first_epoch"] = epochs[0]
        report["last_epoch"] = epochs[-1]
        report["duplicates"] = len(epochs) - len(set(epochs))
        report["ordered"] = epochs == sorted(epochs)
        # trous : pas exact de `granularity` entre 2 bougies consécutives
        report["gaps"] = sum(
            1 for a, b in zip(epochs, epochs[1:]) if b - a != granularity
        )
        # cohérence OHLC
        bad = 0
        for c in candles:
            try:
                o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
                if not (h >= max(o, cl) and l <= min(o, cl) and h >= l
                        and all(v > 0 for v in (o, h, l, cl))):
                    bad += 1
            except (KeyError, TypeError):
                bad += 1
        report["ohlc_errors"] = bad
        # fraîcheur : âge depuis la clôture de la dernière bougie clôturée
        now = int(time.time())
        close_time = epochs[-1] + granularity
        report["age_seconds"] = now - close_time
        report["last_close_utc"] = (
            datetime.fromtimestamp(close_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        )
        report["ok"] = (
            report["ordered"] and report["duplicates"] == 0
            and report["ohlc_errors"] == 0
            # tolérance trous : les synthétiques sont continus, mais un micro-trou
            # réseau isolé ne doit pas bloquer le robot → alerte si > 2 % de trous
            and report["gaps"] <= max(2, len(candles) * 0.02)
            # fraîcheur : dernière clôture < 3 × timeframe (sinon données périmées)
            and (report["age_seconds"] is not None and report["age_seconds"] < 3 * granularity)
        )
        return report

    # ---------------- utilitaire ----------------

    def ping(self) -> float:
        """Test de latence : retourne le temps aller-retour (s) vers Deriv."""
        t0 = time.time()
        self._request({"ping": 1})
        return time.time() - t0
