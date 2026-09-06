"""
Notifications Telegram — formatage HTML + envoi via Bot API (stdlib uniquement).

- format_signal(sig) : message signal (FR) — sens/prix/score/confluences +
  CONTEXTE SYNTHÉTIQUE (obligatoire) + disclaimer. Tout contenu dynamique échappé.
- format_test(settings) : message TEST (connectivité + configuration active).
- send_html(token, chat_id, html) : POST api.telegram.org, parse_mode HTML.
- notify_signals(signals) : envoi groupé, ne lève jamais (erreurs retournées).

Secrets : env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (jamais dans git).
"""
from __future__ import annotations

import html
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

log = logging.getLogger("notify.telegram")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 20

LABELS = {"V10": "Volatility 10 Index", "JD10": "Jump 10 Index",
          "BOOM1000": "Boom 1000 Index"}
SIDES = {"bullish": ("🟢", "ACHAT"), "bearish": ("🔴", "VENTE")}
DISCLAIMER = ("⚠️ <i>Analyse automatique — aucun ordre exécuté. "
              "Ceci n'est pas un conseil financier.</i>")


class TelegramError(RuntimeError):
    """Échec d'envoi (réseau, HTTP ou réponse API ok=false)."""


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=False)


def _fr(s: str) -> str:  # virgule FR + signe moins typographique
    return s.replace(".", ",").replace("-", "−")


def _f2(x: Any) -> str:  # prix, 2 décimales FR
    if x is None:
        return "—"
    try:
        return _fr(f"{float(x):.2f}")
    except (TypeError, ValueError):
        return esc(x)


def _f1(x: Any) -> str:  # points, 1 décimale FR
    if x is None:
        return "—"
    try:
        return _fr(f"{float(x):.1f}")
    except (TypeError, ValueError):
        return esc(x)


def _fi(x: Any) -> str:  # entier arrondi
    if x is None:
        return "—"
    try:
        return f"{float(x):.0f}"
    except (TypeError, ValueError):
        return esc(x)


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _hm(epoch: Any) -> str:
    """Date courte JJ/MM HH:MM (UTC)."""
    if epoch is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%d/%m %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def _synthetic_lines(ctx: Dict[str, Any]) -> List[str]:
    """Puces du CONTEXTE SYNTHÉTIQUE (défensif : clés manquantes = lignes sautées)."""
    lines, ctx = [], ctx or {}
    vol = ctx.get("vol") or {}
    if vol:
        lines.append(f"• Volatilité : <b>{esc(vol.get('regime', '?'))}</b> "
                     f"(ATR M15 {_f1(vol.get('atr_m15'))}, percentile {_fi(vol.get('percentile'))})")
    jump = ctx.get("jump") or {}
    if jump:
        lines.append(f"• Jumps : ~{_f1(jump.get('rate_per_hour'))}/h — médiane {_f1(jump.get('median_size'))} pts "
                     f"(P90 {_f1(jump.get('p90_size'))}, max {_f1(jump.get('max_size'))})")
        lines.append(f"• Dernier jump : il y a {_fi(jump.get('time_since_jump_min'))} min "
                     f"({esc(jump.get('last_jump_direction', '?'))})")
        if ctx.get("jump_risk"):
            lines.append(f"• Risque JUMP : <b>{esc(ctx['jump_risk'])}</b>")
        if ctx.get("jump_risk_detail"):
            lines.append(f"• {esc(ctx['jump_risk_detail'])}")
    boom = ctx.get("boom") or {}
    if boom:
        lines.append(f"• Dernier spike : il y a {_fi(boom.get('time_since_spike_min'))} min "
                     f"(amplitude ~{_f1(boom.get('amplitude_med'))} pts)")
        lines.append(f"• Dérive : {_f1(boom.get('drift_pts_per_hour'))} pts/h · "
                     f"spikes détectés : {_fi(boom.get('n_spikes'))}")
    if ctx.get("rsi_h1") is not None:
        lines.append(f"• RSI H1 : {_f1(ctx['rsi_h1'])}")
    if ctx.get("sl_note"):
        lines.append(f"• {esc(ctx['sl_note'])}")
    if ctx.get("timing"):
        lines.append(f"• {esc(ctx['timing'])}")
    return lines or ["• Contexte indisponible"]


def format_signal(sig: Any) -> str:
    """Message d'entrée : sens + prix + score + contexte synthétique."""
    g = (lambda k, d=None: sig.get(k, d)) if isinstance(sig, dict) else (lambda k, d=None: getattr(sig, k, d))
    emoji, side = SIDES.get(g("direction"), ("⚪", esc(g("direction"))))
    instrument = g("instrument", "?")
    label = LABELS.get(instrument, instrument)
    confs = g("confluences") or []
    lines = [
        f"{emoji} <b>{side} · {esc(instrument)}</b>",
        f"{esc(label)} · {_hm(g('created_epoch', 0))} UTC",
        "",
        f"💰 <b>Entrée :</b> <code>{_f2(g('entry'))}</code>",
        f"🛑 <b>Stop :</b> <code>{_f2(g('sl_price'))}</code> (−{_f1(g('sl_pts'))} pts)",
        f"🎯 <b>Objectif :</b> <code>{_f2(g('tp_price'))}</code> (+{_f1(g('tp_pts'))} pts)",
        f"📐 Ratio 1:3 · 💵 Risque {_f2(g('stake_usd'))} $",
        "",
        f"⭐ <b>{_fi(g('confidence'))}/100 · Grade {esc(g('grade'))}</b>",
        *[f"✅ {esc(c)}" for c in confs],
        "",
        "🧪 <b>CONTEXTE SYNTHÉTIQUE</b>",
        *_synthetic_lines(g("context") or {}),
        "",
        f"🆔 <code>{esc(g('id'))}</code>",
        DISCLAIMER,
    ]
    return "\n".join(lines)


OUTCOMES = {"TP": ("✅", "OBJECTIF ATTEINT"), "SL": ("🛑", "STOP TOUCHÉ"),
            "EXPIRE": ("⌛", "EXPIRE SANS DÉCISION")}


def _duree(bars: Any) -> str:
    """Tenue en clair depuis un nombre de bougies M15."""
    try:
        mins = int(bars or 0) * 15
    except (TypeError, ValueError):
        return "—"
    if mins < 60:
        return f"{mins} min"
    if mins < 1440:
        return f"{mins // 60}h{mins % 60:02d}"
    return f"{mins // 1440}j {(mins % 1440) // 60}h"


def _r_str(r: Any) -> str:
    try:
        v = float(r)
    except (TypeError, ValueError):
        return "—"
    return f"+{_f1(v)}R" if v >= 0 else f"{_f1(v)}R"


def format_outcome(sig: Any, out: Any, stats: dict | None = None) -> str:
    """Message de clôture : résultat + sortie + tenue + R cumulé."""
    gs = (lambda k, d=None: sig.get(k, d)) if isinstance(sig, dict) else (lambda k, d=None: getattr(sig, k, d))
    go = (lambda k, d=None: out.get(k, d)) if isinstance(out, dict) else (lambda k, d=None: getattr(out, k, d))
    emoji, word = OUTCOMES.get(go("result"), ("⚪", esc(go("result"))))
    instrument = gs("instrument", "?")
    side = SIDES.get(gs("direction"), ("", "?"))[1]
    pts = go("points")
    try:
        pts_str = f"+{_f1(pts)} pts" if float(pts) >= 0 else f"{_f1(pts)} pts"
    except (TypeError, ValueError):
        pts_str = "—"
    st = stats or {}
    wr = st.get("winrate")
    wr_str = f"{100 * wr:.0f} %" if isinstance(wr, (int, float)) else "—"
    lines = [
        f"{emoji} <b>{word} · {esc(instrument)} {esc(side)}</b>",
        f"💰 Entrée <code>{_f2(gs('entry'))}</code> → Sortie <code>{_f2(go('exit_price'))}</code>",
        f"📊 Résultat : <b>{_r_str(go('r'))}</b> ({pts_str})",
        f"⏱️ Tenue : {_duree(go('bars_held'))} ({_fi(go('bars_held'))} × M15)",
        f"📈 Cumul : <b>{_r_str(st.get('r_total'))}</b> · {_fi(st.get('TP'))} TP / {_fi(st.get('SL'))} SL · winrate {wr_str}",
        "",
        f"🆔 <code>{esc(gs('id'))}</code> · clôturé le {_hm(go('closed_epoch'))} UTC",
    ]
    return "\n".join(lines)


def format_test(settings: Dict[str, Any]) -> str:
    """Message TEST : connectivité + configuration active (jamais de secret)."""
    insts = settings.get("instruments", {})
    on = [n for n, i in insts.items() if i.get("enabled", True)]
    off = [n for n, i in insts.items() if not i.get("enabled", True)]
    sc = settings.get("scoring", {})
    stake = settings.get("account", {}).get("stake_usd", "?")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "✅ <b>TEST — Robot signaux Deriv</b>",
        f"🕒 {esc(now)}",
        "🤖 Connexion bot OK · format HTML OK.",
        f"📊 Actifs : <b>{esc(', '.join(on) or 'aucun')}</b>"
        + (f" · ⏸️ en pause : {esc(', '.join(off))}" if off else ""),
        f"🎯 Seuil {esc(sc.get('threshold', '?'))} · cooldown "
        f"{esc(sc.get('cooldown_minutes', '?'))} min · max "
        f"{esc(sc.get('max_per_day_per_instrument', '?'))}/j/instrument · "
        f"stake {_f2(stake)} $",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def send_html(token: str, chat_id: str, text: str) -> dict:
    """Envoie un message HTML. Retourne le `result` API. Lève TelegramError."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    req = urllib.request.Request(API_URL.format(token=token),
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise TelegramError(f"HTTP {e.code} : {body}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TelegramError(f"réseau : {e}")
    if not data.get("ok"):
        raise TelegramError(f"API ok=false : {data.get('description', data)}")
    return data.get("result", {})


def notify_signals(signals: List[Any]) -> List[dict]:
    """Envoie chaque signal. Ne lève jamais : 1 dict résultat par signal."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    g = lambda s, k: s.get(k) if isinstance(s, dict) else getattr(s, k, "?")
    if not token or not chat:
        return [{"signal_id": g(s, "id"), "instrument": g(s, "instrument"),
                 "status": "skipped",
                 "reason": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents"} for s in signals]
    out = []
    for s in signals:
        try:
            r = send_html(token, chat, format_signal(s))
            out.append({"signal_id": g(s, "id"), "instrument": g(s, "instrument"),
                        "status": "sent", "message_id": r.get("message_id")})
        except TelegramError as e:
            log.warning("envoi %s échoué : %s", g(s, "id"), e)
            out.append({"signal_id": g(s, "id"), "instrument": g(s, "instrument"),
                        "status": "error", "error": str(e)})
    return out


def notify_closes(items: List[tuple], stats: dict | None = None) -> List[dict]:
    """Envoie chaque clôture (signal, outcome). Ne lève jamais."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    def g(obj, k):
        return obj.get(k) if isinstance(obj, dict) else getattr(obj, k, "?")

    if not token or not chat:
        return [{"signal_id": g(o, "signal_id"), "instrument": g(s, "instrument"),
                 "status": "skipped",
                 "reason": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents"}
                for s, o in items]
    out = []
    for sig, oc in items:
        try:
            r = send_html(token, chat, format_outcome(sig, oc, stats))
            out.append({"signal_id": g(oc, "signal_id"), "instrument": g(sig, "instrument"),
                        "status": "sent", "message_id": r.get("message_id")})
        except TelegramError as e:
            log.warning("envoi clôture %s échoué : %s", g(oc, "signal_id"), e)
            out.append({"signal_id": g(oc, "signal_id"), "instrument": g(sig, "instrument"),
                        "status": "error", "error": str(e)})
    return out

