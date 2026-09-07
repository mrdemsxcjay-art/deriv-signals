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

LABELS = {"JD10": "Jump 10 Index",
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
        if jump.get("n_jumps") is not None:
            lines.append(f"• Jumps détectés : {_fi(jump.get('n_jumps'))}")
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
        if boom.get("avg_interval_min") is not None:
            lines.append(f"• Intervalle spikes : moyen {_f1(boom.get('avg_interval_min'))} min "
                         f"(médian {_f1(boom.get('median_interval_min'))})")
        if boom.get("amplitude_p90") is not None:
            lines.append(f"• Amplitude spikes : P90 {_f1(boom.get('amplitude_p90'))} pts "
                         f"(max {_f1(boom.get('amplitude_max'))})")
        if boom.get("dist_to_spike") is not None:
            lines.append(f"• Distance au dernier spike : {_f1(boom.get('dist_to_spike'))} pts")
    if ctx.get("rsi_h1") is not None:
        lines.append(f"• RSI H1 : {_f1(ctx['rsi_h1'])}")
    if ctx.get("sl_note"):
        lines.append(f"• {esc(ctx['sl_note'])}")
    if ctx.get("timing"):
        lines.append(f"• {esc(ctx['timing'])}")
    return lines or ["• Contexte indisponible"]


def _as_list(raw: Any) -> List[Any]:
    """Normalise gates/confluences : Signal (liste) ou ligne base (JSON str)."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    return list(raw) if isinstance(raw, (list, tuple)) else []


def _as_ctx(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _gates_lines(gates: List[Any]) -> List[str]:
    out = []
    for gt in gates:
        if isinstance(gt, dict):
            name, ok, detail = gt.get("name", "?"), gt.get("passed"), gt.get("detail", "")
        else:
            name = getattr(gt, "name", "?")
            ok, detail = getattr(gt, "passed", None), getattr(gt, "detail", "")
        mark = "✅" if ok else ("❌" if ok is False else "⚪")
        out.append(f"{mark} {esc(name)} : {esc(detail)}" if detail else f"{mark} {esc(name)}")
    return out


def format_signal(sig: Any) -> str:
    """Message d'entrée détaillé : plan + score + portes + confluences + contexte."""
    g = (lambda k, d=None: sig.get(k, d)) if isinstance(sig, dict) else (lambda k, d=None: getattr(sig, k, d))
    emoji, side = SIDES.get(g("direction"), ("⚪", esc(g("direction"))))
    instrument = g("instrument", "?")
    label = LABELS.get(instrument, instrument)
    confs = _as_list(g("confluences")) or _as_list(g("confluences_json"))
    gates = _as_list(g("gates")) or _as_list(g("gates_json"))
    ctx = _as_ctx(g("context")) or _as_ctx(g("context_json"))
    try:
        ratio = float(g("tp_pts")) / float(g("sl_pts"))
        gain_line = (f"💰 <b>Gain potentiel :</b> +{_f2(float(g('stake_usd')) * ratio)} $ "
                     f"({_f1(ratio)}R)")
    except (TypeError, ValueError, ZeroDivisionError):
        gain_line = ""
    bd = ctx.get("score_breakdown") or {}
    parts = " · ".join(f"{k} +{bd[k]}" for k in
                       ("base", "zone", "confirmation", "contexte", "régime", "force", "rsi")
                       if isinstance(bd, dict) and k in bd)
    bd_line = f"🧮 Détail : {esc(parts)}" if parts else ""
    bar_line = (f"🕒 Barre M15 : {_hm(g('entry_epoch'))} UTC" if g("entry_epoch") else "")
    lines = [
        f"{emoji} <b>{side} · {esc(instrument)}</b>",
        f"{esc(label)} · {_hm(g('created_epoch', 0))} UTC",
        *([bar_line] if bar_line else []),
        "",
        f"💰 <b>Entrée :</b> <code>{_f2(g('entry'))}</code>",
        f"🛑 <b>Stop :</b> <code>{_f2(g('sl_price'))}</code> (−{_f1(g('sl_pts'))} pts)",
        f"🎯 <b>Objectif :</b> <code>{_f2(g('tp_price'))}</code> (+{_f1(g('tp_pts'))} pts)",
        f"📐 Ratio 1:3 · 💵 Risque {_f2(g('stake_usd'))} $",
        *([gain_line] if gain_line else []),
        "",
        f"⭐ <b>{_fi(g('confidence'))}/100 · Grade {esc(g('grade'))}</b>",
        *([bd_line] if bd_line else []),
    ]
    if gates:
        lines += ["", "🚪 <b>PORTES</b>", *_gates_lines(gates)]
    gdetails = set()
    for gt in gates:
        dt = gt.get("detail", "") if isinstance(gt, dict) else getattr(gt, "detail", "")
        if dt:
            gdetails.add(str(dt).strip())
    confs = [c for c in confs if str(c).strip() not in gdetails]
    if confs:
        lines += ["", *[f"✅ {esc(c)}" for c in confs]]
    lines += ["", "🧪 <b>CONTEXTE SYNTHÉTIQUE</b>", *_synthetic_lines(ctx),
              "", f"🆔 <code>{esc(g('id'))}</code>", DISCLAIMER]
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
    targets = ""
    if gs("sl_price") is not None or gs("tp_price") is not None:
        targets = (f"🛑 Stop <code>{_f2(gs('sl_price'))}</code> (−{_f1(gs('sl_pts'))} pts) · "
                   f"🎯 Objectif <code>{_f2(gs('tp_price'))}</code> (+{_f1(gs('tp_pts'))} pts)")
    setup = ""
    if gs("confidence") is not None:
        setup = f"⭐ Signal d'origine : {_fi(gs('confidence'))}/100 · Grade {esc(gs('grade'))}"
    period = ""
    if gs("created_epoch") is not None and go("closed_epoch") is not None:
        period = f"🕒 Du {_hm(gs('created_epoch'))} au {_hm(go('closed_epoch'))} UTC"
    note_line = f"📝 Note : {esc(go('note'))}" if go("note") else ""
    cumul = (f"📈 Cumul : <b>{_r_str(st.get('r_total'))}</b> · {_fi(st.get('TP'))} TP / "
             f"{_fi(st.get('SL'))} SL · winrate {wr_str}")
    extra = []
    if st.get("EXPIRE"):
        extra.append(f"{_fi(st.get('EXPIRE'))} EXPIRE")
    if st.get("n"):
        extra.append(f"{_fi(st.get('n'))} clôturé{'s' if st.get('n') != 1 else ''}")
    if st.get("r_avg") is not None:
        extra.append(f"moy {_r_str(st.get('r_avg'))}/trade")
    if extra:
        cumul += " · " + " · ".join(extra)
    lines = [
        f"{emoji} <b>{word} · {esc(instrument)} {esc(side)}</b>",
        f"💰 Entrée <code>{_f2(gs('entry'))}</code> → Sortie <code>{_f2(go('exit_price'))}</code>",
        *([targets] if targets else []),
        f"📊 Résultat : <b>{_r_str(go('r'))}</b> ({pts_str})",
        *([setup] if setup else []),
        f"⏱️ Tenue : {_duree(go('bars_held'))} ({_fi(go('bars_held'))} × M15)",
        *([period] if period else []),
        *([note_line] if note_line else []),
        cumul,
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

