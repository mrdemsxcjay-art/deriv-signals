"""
Test ÉTAPE 5 — Telegram : formatage HTML (sections obligatoires, échappement),
message TEST, envoi mocké (0 réseau par défaut ; réel si TELEGRAM_LIVE_TEST=1).

Exécution : python scripts/test_notify.py
"""
import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.notify.telegram import (  # noqa: E402
    TelegramError, format_outcome, format_signal, format_test, notify_closes,
    notify_signals, send_html,
)

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


JD10_SIG = {
    "id": "JD10-bearish-1725630000", "instrument": "JD10", "symbol": "JD10",
    "direction": "bearish", "created_epoch": 1725630000, "entry": 62345.678,
    "sl_pts": 155.7, "tp_pts": 467.1, "sl_price": 62501.378, "tp_price": 61878.578,
    "stake_usd": 1.0, "confidence": 79, "grade": "A",
    "confluences": ["cassure M15 baissière (âge 1)", "retest OB H1"],
    "context": {
        "sl_note": "SL JD10 = max(plancher 40, 4,0×jump médian 155.7) = 155.7 pts",
        "rsi_h1": 31.04, "timing": "timing : aucune confirmation M5/M30",
        "vol": {"atr_m15": 101.29, "percentile": 85.0, "regime": "tendu"},
        "jump": {"rate_per_hour": 2.7, "median_size": 38.9, "p90_size": 86.9,
                 "max_size": 101.8, "time_since_jump_min": 10.5,
                 "last_jump_direction": "DN"},
        "jump_risk": "FAIBLE", "jump_risk_detail": "SL 155.7 ≥ P90 86.9.",
    },
}

BOOM_SIG = {
    "id": "BOOM1000-bullish-1725630900", "instrument": "BOOM1000",
    "symbol": "BOOM1000", "direction": "bullish", "created_epoch": 1725630900,
    "entry": 15037.08, "sl_pts": 25.0, "tp_pts": 75.0,
    "sl_price": 15012.08, "tp_price": 15112.08,
    "stake_usd": 1.0, "confidence": 82, "grade": "A",
    "confluences": ["spike récent + pin M5", "<b>INJECTION</b> & \"test\""],
    "context": {
        "sl_note": "SL plancher BOOM1000 = 25 pts", "rsi_h1": 43.55,
        "timing": "timing : englobante haussier M5",
        "vol": {"atr_m15": 19.4, "percentile": 62.8, "regime": "normal"},
        "boom": {"time_since_spike_min": 5.0, "amplitude_med": 14.8,
                 "drift_pts_per_hour": -4.03, "n_spikes": 1},
    },
}

SETTINGS = {"instruments": {"V10": {"enabled": False}, "JD10": {"enabled": True},
                            "BOOM1000": {"enabled": True}},
            "scoring": {"threshold": 65, "cooldown_minutes": 180,
                        "max_per_day_per_instrument": 4},
            "account": {"stake_usd": 1.0}}


@check("1/8 Signal JD10 — sections obligatoires + nombres FR")
def t_jd10():
    m = format_signal(JD10_SIG)
    for needle in ("🔴", "VENTE · JD10", "Jump 10 Index", "UTC", "Entrée", "62345,68",
                   "Stop", "−155,7", "Objectif", "+467,1", "Ratio 1:3",
                   "Risque 1,00 $", "79/100 · Grade A", "✅ cassure M15",
                   "CONTEXTE SYNTHÉTIQUE", "tendu", "101,3", "~2,7/h", "P90 86,9",
                   "il y a 10 min (DN)", "Risque JUMP", "FAIBLE", "RSI H1 : 31,0",
                   "aucun ordre exécuté", "JD10-bearish-1725630000"):
        assert needle in m, needle
    print("   25 marqueurs présents ✅")


@check("2/8 Signal BOOM — ACHAT + échappement anti-injection")
def t_boom():
    m = format_signal(BOOM_SIG)
    for needle in ("🟢", "ACHAT · BOOM1000", "Boom 1000 Index", "15037,08",
                   "82/100 · Grade A", "✅ spike récent", "il y a 5 min",
                   "−4,0 pts/h", "spikes détectés : 1"):
        assert needle in m, needle
    assert "<b>INJECTION</b>" not in m and "&lt;b&gt;INJECTION&lt;/b&gt;" in m
    assert "&amp;" in m  # & échappé
    # contexte vide = fallback, jamais de crash
    m2 = format_signal({**BOOM_SIG, "context": {}})
    assert "Contexte indisponible" in m2
    print("   injection neutralisée + fallback contexte ✅")


@check("3/8 Message TEST — actifs/pause/seuils, sans secret")
def t_test():
    m = format_test(SETTINGS)
    for needle in ("TEST", "Robot signaux Deriv", "Connexion bot OK",
                   "JD10", "BOOM1000", "⏸️ en pause", "V10",
                   "Seuil 65", "cooldown 180", "4/j/instrument", "1,00 $",
                   "aucun ordre exécuté"):
        assert needle in m, needle
    assert "TELEGRAM" not in m and "token" not in m.lower()
    m_all = format_test({"instruments": {"JD10": {}}, "scoring": {}, "account": {}})
    assert "en pause" not in m_all  # rien à signaler si tout actif
    print("   TEST complet, 0 secret ✅")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@check("4/8 Envoi mocké — payload API + 3 chemins d'erreur")
def t_send():
    import urllib.request
    real = urllib.request.urlopen
    seen = {}

    def fake_ok(req, timeout=None):
        seen.update(json.loads(req.data.decode()))
        seen["_url"] = req.full_url
        return FakeResp(json.dumps({"ok": True, "result": {"message_id": 42}}).encode())

    urllib.request.urlopen = fake_ok
    try:
        r = send_html("TOK", "123", "<b>hi</b>")
        assert r == {"message_id": 42}, r
        assert seen["chat_id"] == "123" and seen["parse_mode"] == "HTML"
        assert seen["text"] == "<b>hi</b>" and seen["disable_web_page_preview"] is True
        assert seen["_url"] == "https://api.telegram.org/botTOK/sendMessage"

        def fake_api_err(req, timeout=None):
            return FakeResp(json.dumps({"ok": False, "description": "chat not found"}).encode())
        urllib.request.urlopen = fake_api_err
        try:
            send_html("TOK", "123", "x")
            raise SystemExit("aurait dû lever !")
        except TelegramError as e:
            assert "chat not found" in str(e)

        def fake_http(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"bad token"))
        urllib.request.urlopen = fake_http
        try:
            send_html("BAD", "123", "x")
            raise SystemExit("aurait dû lever !")
        except TelegramError as e:
            assert "401" in str(e)
    finally:
        urllib.request.urlopen = real
    print("   payload OK + ok=false + HTTP 401 ✅")


@check("5/8 notify_signals — skip sans env, sent mocké, jamais de crash")
def t_notify():
    import urllib.request
    real_urlopen, real_env = urllib.request.urlopen, dict(os.environ)
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        out = notify_signals([JD10_SIG, BOOM_SIG])
        assert [r["status"] for r in out] == ["skipped", "skipped"], out
        assert "TELEGRAM_BOT_TOKEN" in out[0]["reason"]

        os.environ["TELEGRAM_BOT_TOKEN"] = "TOK"
        os.environ["TELEGRAM_CHAT_ID"] = "123"
        urllib.request.urlopen = lambda req, timeout=None: FakeResp(
            json.dumps({"ok": True, "result": {"message_id": 7}}).encode())
        out = notify_signals([JD10_SIG])
        assert out[0]["status"] == "sent" and out[0]["message_id"] == 7, out

        def boom(req, timeout=None):
            raise ConnectionError("coupure !")
        urllib.request.urlopen = boom  # erreur non-Telegram → error, pas de raise
        out = notify_signals([JD10_SIG])
        assert out[0]["status"] == "error" and "coupure" in out[0]["error"], out
    finally:
        urllib.request.urlopen = real_urlopen
        os.environ.clear()
        os.environ.update(real_env)
    print("   skipped/sent/error ✅")


@check("6/8 Wiring moteur — notify=True envoie + 📩, False silencieux (régression)")
def t_wiring():
    import tempfile
    from src.agents.strategy_agent import Decision, GateResult
    from src.notify import telegram as TG
    from src.signals import engine as ENG
    tiny = [{"epoch": 10 + i, "open": 10.0, "high": 10.1, "low": 9.9,
             "close": 10.05} for i in range(3)]
    fake_tf = {k: tiny for k in ("D1", "H4", "H1", "M30", "M15", "M5")}

    class FakeProvider:
        def get_timeframe(self, symbol, tf, count):
            return fake_tf[tf]

        def get_candles(self, symbol, gran, count):
            return fake_tf["M15"]

        def get_ticks(self, symbol, count=2000, end="latest", use_cache=None,
                      max_cache=40000):
            return []

        def close(self):
            pass

    NOON = (1_700_000_000 // 86400) * 86400 + 12 * 3600
    settings = {"instruments": {"JD10": {"symbol": "JD10"},
                                "BOOM1000": {"symbol": "BOOM1000"}},
                "timeframes": {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
                               "H4": 14400, "D1": 86400},
                "counts": {"M5": 3, "M15": 3, "M30": 3, "H1": 3, "H4": 3, "D1": 3},
                "strategy": {}, "synthetics": {},
                "stops": {"JD10": 40.0, "BOOM1000": 25.0},
                "scoring": {"threshold": 65, "cooldown_minutes": 180,
                            "max_per_day_per_instrument": 4},
                "account": {"stake_usd": 1.0}}
    fake_dec = Decision("X", "bearish", True,
                        gates=[GateResult("D1", "bearish", True, "porte factice")],
                        score=78, grade="A", breakdown={"base": 50},
                        plan={"entry": 10.0, "sl_pts": 25.0, "tp_pts": 75.0,
                              "sl_price": 35.0, "tp_price": -65.0,
                              "entry_epoch": NOON},
                        confluences=["factice"], context_snapshot={})
    calls = []

    def fake_notify(sigs):
        calls.extend(sigs)
        return [{"signal_id": s.id, "instrument": s.instrument,
                 "status": "sent"} for s in sigs]

    real_eval, real_notify = ENG.evaluate_instrument, TG.notify_signals
    ENG.evaluate_instrument = lambda *a, **k: fake_dec
    TG.notify_signals = fake_notify
    try:
        tmp = os.path.join(tempfile.mkdtemp(), "w.db")
        res = ENG.run_cycle(settings, db_path=tmp, provider=FakeProvider(),
                            now_epoch=NOON, notify=True)
        assert len(res.signals) == 2 and len(calls) == 2, (res.signals, calls)
        assert all(v.endswith("📩") for v in res.logs.values()), res.logs
        assert res.errors == [], res.errors
        res2 = ENG.run_cycle(settings, db_path=tmp, provider=FakeProvider(),
                             now_epoch=NOON + 200 * 60, notify=False)
        assert len(res2.signals) == 2 and len(calls) == 2  # silencieux
    finally:
        ENG.evaluate_instrument, TG.notify_signals = real_eval, real_notify
    print("   2 signaux → 2 envois + 📩 · notify=False silencieux ✅")


OUTCOME_SIG = {"id": "BOOM1000-bearish-1725630900", "instrument": "BOOM1000",
               "direction": "bearish", "entry": 14681.07}
OUTCOME_STATS = {"TP": 12, "SL": 28, "EXPIRE": 5, "winrate": 0.3, "r_total": 4.2}


@check("7/8 Clôture — TP/SL/EXPIRE + tenue + cumul + escape")
def t_outcome():
    tp = format_outcome(OUTCOME_SIG, {"signal_id": OUTCOME_SIG["id"], "result": "TP",
        "r": 3.0, "points": 75.0, "bars_held": 5, "exit_price": 14606.07,
        "closed_epoch": 1725635400}, OUTCOME_STATS)
    for needle in ("✅", "OBJECTIF ATTEINT", "BOOM1000", "VENTE", "14681,07",
                   "14606,07", "+3,0R", "+75,0 pts", "1h15", "5 × M15", "Cumul",
                   "+4,2R", "12 TP / 28 SL", "winrate 30 %", "clôturé le",
                   "BOOM1000-bearish-1725630900"):
        assert needle in tp, needle
    sl = format_outcome(OUTCOME_SIG, {"signal_id": OUTCOME_SIG["id"], "result": "SL",
        "r": -1.0, "points": -25.0, "bars_held": 2, "exit_price": 14706.07,
        "closed_epoch": 1725632700},
        {"TP": 0, "SL": 1, "winrate": 0.0, "r_total": -1.0})
    for needle in ("🛑", "STOP TOUCHÉ", "−1,0R", "−25,0 pts", "30 min", "2 × M15",
                   "0 TP / 1 SL", "winrate 0 %"):
        assert needle in sl, needle
    ex = format_outcome(OUTCOME_SIG, {"signal_id": OUTCOME_SIG["id"], "result": "EXPIRE",
        "r": 0.2, "points": 5.1, "bars_held": 100, "exit_price": 14675.97,
        "closed_epoch": 1725720000}, OUTCOME_STATS)
    for needle in ("⌛", "EXPIRE SANS DÉCISION", "+0,2R", "1j 1h", "100 × M15"):
        assert needle in ex, needle
    m2 = format_outcome({"id": "<x>", "instrument": "JD10", "direction": "bullish",
                         "entry": 1.0},
                        {"result": "SL", "r": -1, "points": -1, "bars_held": 1,
                         "exit_price": 2.0}, None)
    assert "<x>" not in m2 and "&lt;x&gt;" in m2 and "winrate —" in m2
    print("   TP/SL/EXPIRE + durées + cumul + escape ✅")


@check("8/8 Wiring clôtures — tracker stub → notify_closes + 📪 (ERROR ignorée)")
def t_close_wiring():
    import tempfile
    from src.agents.strategy_agent import Decision
    from src.notify import telegram as TG
    from src.signals import engine as ENG
    from src.storage import database as db
    tiny = [{"epoch": 10 + i, "open": 10.0, "high": 10.1, "low": 9.9,
             "close": 10.05} for i in range(3)]
    fake_tf = {k: tiny for k in ("D1", "H4", "H1", "M30", "M15", "M5")}

    class FakeProvider:
        def get_timeframe(self, symbol, tf, count):
            return fake_tf[tf]

        def get_candles(self, symbol, gran, count):
            return fake_tf["M15"]

        def get_ticks(self, symbol, count=2000, end="latest", use_cache=None,
                      max_cache=40000):
            return []

        def close(self):
            pass

    NOON = (1_700_000_000 // 86400) * 86400 + 12 * 3600
    settings = {"instruments": {"BOOM1000": {"symbol": "BOOM1000"}},
                "timeframes": {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
                               "H4": 14400, "D1": 86400},
                "counts": {"M5": 3, "M15": 3, "M30": 3, "H1": 3, "H4": 3, "D1": 3},
                "strategy": {}, "synthetics": {}, "stops": {"BOOM1000": 25.0},
                "scoring": {"threshold": 65, "cooldown_minutes": 180,
                            "max_per_day_per_instrument": 4},
                "account": {"stake_usd": 1.0}}
    tmp = os.path.join(tempfile.mkdtemp(), "c.db")
    db.init_db(tmp)
    db.save_signal(tmp, {"id": "BOOM1000-bearish-1", "instrument": "BOOM1000",
                         "symbol": "BOOM1000", "direction": "bearish",
                         "created_epoch": NOON, "entry_epoch": NOON, "entry": 100.0,
                         "sl_pts": 25.0, "tp_pts": 75.0, "sl_price": 125.0,
                         "tp_price": 25.0, "stake_usd": 1.0, "confidence": 75,
                         "grade": "A"})
    close = {"signal_id": "BOOM1000-bearish-1", "result": "SL", "r": -1.0,
             "points": -25.0, "bars_held": 5, "exit_price": 125.0,
             "closed_epoch": NOON + 4500, "note": ""}
    err = {"signal_id": "*", "result": "ERROR", "r": 0.0, "points": 0.0,
           "bars_held": 0, "exit_price": 0.0, "closed_epoch": 0, "note": "panne"}
    calls = []

    def fake_closes(items, stats=None):
        calls.extend(items)
        return [{"signal_id": o["signal_id"], "instrument": s["instrument"],
                 "status": "sent"} for s, o in items]

    real_eval, real_trk, real_nc = (ENG.evaluate_instrument, ENG.tracker_update,
                                    TG.notify_closes)
    ENG.evaluate_instrument = lambda *a, **k: Decision("BOOM1000", "bearish", False,
                                                       blocked_by="test")
    ENG.tracker_update = lambda *a, **k: [close, err]
    TG.notify_closes = fake_closes
    try:
        res = ENG.run_cycle(settings, db_path=tmp, provider=FakeProvider(),
                            now_epoch=NOON + 5400, notify=True)
        assert len(calls) == 1 and calls[0][1]["result"] == "SL", calls
        assert calls[0][0]["entry"] == 100.0  # signal relu depuis la base
        assert res.logs["BOOM1000"].endswith("📪"), res.logs
        assert res.signals == [] and res.errors == [], (res.signals, res.errors)
    finally:
        ENG.evaluate_instrument, ENG.tracker_update, TG.notify_closes = \
            real_eval, real_trk, real_nc
    print("   1 clôture notifiée + 📪 · ERROR ignorée · 0 entrée ✅")


def main():
    print("=" * 64)
    print("TEST ÉTAPE 5 — Telegram (format + TEST + envoi)")
    print("=" * 64)
    if os.environ.get("TELEGRAM_LIVE_TEST") == "1":
        print("\n▶ ENVOI RÉEL demandé (TELEGRAM_LIVE_TEST=1)")
        out = notify_signals([JD10_SIG])
        print(f"   → {out}")
        assert out and out[0]["status"] == "sent", out
        print("  → ENVOI RÉEL : PASS ✅")
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
