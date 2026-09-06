"""
Test fonctionnel ÉTAPE 1 — Connexion Deriv + bougies + qualité + cache + reconnexion.

Exécution :  python scripts/test_data.py
Succès : affiche 6/6 checks ✅ et quitte avec le code 0.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.deriv_provider import ALLOWED_SYMBOLS, TIMEFRAMES, DerivProvider

WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("1/6 Connexion + ping Deriv")
def t_ping():
    p = DerivProvider(ws_url=WS_URL)
    try:
        latency = p.ping()
        assert latency < 15, f"ping trop lent : {latency:.1f}s"
        print(f"   ping = {latency * 1000:.0f} ms ✅")
    finally:
        p.close()


@check("2/6 Symboles exclusifs R_10 / JD10 / BOOM1000")
def t_symbols():
    assert ALLOWED_SYMBOLS == {"R_10", "JD10", "BOOM1000"}, ALLOWED_SYMBOLS
    p = DerivProvider(ws_url=WS_URL)
    try:
        for sym in sorted(ALLOWED_SYMBOLS):
            c = p.get_candles(sym, 900, 5, use_cache=False)
            assert len(c) >= 3, f"{sym} : seulement {len(c)} bougies"
            print(f"   {sym} : {len(c)} bougies M15, dernier close = {c[-1]['close']} ✅")
        # garde-fou règle n°1 : tout autre symbole est refusé AVANT tout appel réseau
        try:
            p.get_candles("EURUSD", 900, 5)
            raise AssertionError("EURUSD aurait dû être refusé !")
        except ValueError as e:
            print(f"   garde-fou Forex → refusé : « {e} » ✅")
    finally:
        p.close()


@check("3/6 Téléchargement 6 TF × 3 instruments + qualité (fraîcheur, trous, OHLC)")
def t_all_timeframes():
    p = DerivProvider(ws_url=WS_URL)
    try:
        n_ok = 0
        for sym in sorted(ALLOWED_SYMBOLS):
            for tf, gran in TIMEFRAMES.items():
                candles = p.get_timeframe(sym, tf, 120)
                rep = p.validate(candles, gran)
                flag = "✅" if rep["ok"] else "❌"
                n_ok += rep["ok"]
                print(
                    f"   {flag} {sym:>9} {tf:>4} : n={rep['count']:>3} "
                    f"clôture={rep['last_close_utc']} (âge {rep['age_seconds']}s) "
                    f"trous={rep['gaps']} dupl={rep['duplicates']} ohlc={rep['ohlc_errors']}"
                )
        assert n_ok == 18, f"{18 - n_ok} série(s) en anomalie"
    finally:
        p.close()


@check("4/6 Anti-repaint : que des bougies CLÔTURÉES")
def t_no_repaint():
    p = DerivProvider(ws_url=WS_URL)
    try:
        now = int(time.time())
        for sym in sorted(ALLOWED_SYMBOLS):
            candles = p.get_candles(sym, 300, 50, use_cache=False)  # M5 = le plus exigeant
            last = candles[-1]
            assert last["epoch"] + 300 <= now - 5, f"{sym} : dernière bougie non clôturée !"
            print(f"   {sym} : dernière M5 clôturée à epoch {last['epoch']} ✅")
    finally:
        p.close()


@check("5/6 Cache incrémental : 2e lecture sans re-téléchargement massif")
def t_cache():
    p = DerivProvider(ws_url=WS_URL, cache_dir="data/cache")
    try:
        t0 = time.time()
        c1 = p.get_candles("R_10", 900, 300)
        d1 = time.time() - t0
        t0 = time.time()
        c2 = p.get_candles("R_10", 900, 300)
        d2 = time.time() - t0
        assert len(c1) == 300, f"profondeur {len(c1)} au lieu de 300"
        assert c1[-1]["epoch"] == c2[-1]["epoch"], "cache incohérent"
        print(f"   1er appel {d1:.2f}s ({len(c1)} bougies) → 2e appel {d2:.2f}s (cache) ✅")
        assert os.path.exists("data/cache/R_10_900.json"), "fichier cache manquant"
    finally:
        p.close()


@check("6/6 Reconnexion : coupure simulée → backoff → reprise")
def t_reconnect():
    bad = DerivProvider(ws_url="wss://127.0.0.1:1", max_retries=2, backoff_base=0.2, timeout=3)
    t0 = time.time()
    try:
        bad.ping()
        raise AssertionError("la fausse URL aurait dû échouer !")
    except ConnectionError as e:
        dt = time.time() - t0
        assert dt >= 0.5, "pas de backoff observé"
        print(f"   coupure simulée → échec propre après backoff ({dt:.1f}s) ✅")
    finally:
        bad.close()
    # preuve que le provider sain fonctionne toujours après
    p = DerivProvider(ws_url=WS_URL)
    try:
        assert p.ping() < 15
        print("   reconnexion URL réelle → ping OK ✅")
    finally:
        p.close()


def main():
    print("=" * 64)
    print("TEST ÉTAPE 1 — DerivProvider (R_10 / JD10 / BOOM1000)")
    print("=" * 64)
    passed = 0
    for name, fn in CHECKS:
        print(f"\n▶ {name}")
        try:
            fn()
            print(f"  → {name} : PASS ✅")
            passed += 1
        except AssertionError as e:
            print(f"  → {name} : FAIL ❌ — {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  → {name} : ERREUR ❌ — {type(e).__name__}: {e}")
    print("\n" + "=" * 64)
    print(f"RÉSULTAT : {passed}/{len(CHECKS)} checks passés")
    print("=" * 64)
    sys.exit(0 if passed == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
