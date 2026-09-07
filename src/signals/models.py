"""
Modèles de signaux — objets échangés entre stratégie, moteur, tracker et (étape 5) Telegram.

- direction interne : "bullish" | "bearish" (mappée en ACHAT/VENTE à l'affichage).
- R par construction : TP = +3, SL = −1, EXPIRE = R latent clampé ⇒ bornes [-1, +3].
- TP = 3 × risque exactement (ratio 1:3 sacré, §7) ; SL élargissable, jamais réduit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


def utc_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class Signal:
    id: str                      # "{INSTRUMENT}-{bullish|bearish}-{epoch_barre_M15}" (C1 : déterministe)
    instrument: str              # "V10" | "JD10" | "BOOM1000"
    symbol: str                  # "R_10" | "JD10" | "BOOM1000"
    direction: str               # "bullish" | "bearish"
    created_epoch: int
    entry_epoch: int             # epoch de la bougie M15 de référence (suivi tracker)
    entry: float
    sl_pts: float
    tp_pts: float                # == 3 × sl_pts exactement
    sl_price: float
    tp_price: float
    stake_usd: float             # fixe (1 $), jamais recalculée
    confidence: int              # /100
    grade: str                   # "B" | "A" | "A+"
    gates: List[dict] = field(default_factory=list)       # D1/H4/H1/M15 (+timing)
    confluences: List[str] = field(default_factory=list)  # puces humaines (message)
    context: dict = field(default_factory=dict)           # snapshot contexte §5
    status: str = "ACTIVE"       # ACTIVE | CLOSED

    @property
    def created_utc(self) -> str:
        return utc_iso(self.created_epoch)

    @property
    def side_label(self) -> str:
        return "ACHAT" if self.direction == "bullish" else "VENTE"


@dataclass
class Outcome:
    signal_id: str
    closed_epoch: int
    result: str                  # "TP" | "SL" | "EXPIRE"
    r: float                     # dans [-1, +3] (garanti par le tracker)
    points: float
    bars_held: int               # bougies M15 entre entrée et sortie
    exit_price: float
    note: str = ""

    @property
    def closed_utc(self) -> str:
        return utc_iso(self.closed_epoch)
