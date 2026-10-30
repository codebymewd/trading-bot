"""Prop-firm rules import: text (or OCR/PDF text) -> structured draft -> user edits -> user confirms.

Principles: never guess a missing value, flag ambiguous or conflicting statements, never activate anything the
user has not confirmed, and say plainly which rules the bot cannot enforce yet. Extraction is regex-based and
assistive only: the user must review every value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config_schema import PropProfile
from .trading_day import TimezoneError, resolve_fixed_offset

REQUIRED = ("starting_balance", "daily_loss_limit_percent", "max_drawdown_percent", "drawdown_type",
            "drawdown_basis", "daily_reset_time", "daily_reset_timezone")
NOT_ENFORCED = {"consistency": "consistency rule", "min_trading_days": "minimum trading days",
                "overnight": "overnight/weekend holding rules"}
TZ_MAP = {"UTC": "UTC", "GMT": "UTC", "WAT": "Africa/Lagos", "LAGOS": "Africa/Lagos", "NIGERIA": "Africa/Lagos"}
# Abbreviations that observe DST or are ambiguous are deliberately NOT mapped: the user must choose.


class OcrUnavailable(RuntimeError):
    pass


def extract_text(path: str) -> str:
    """Text from a screenshot (OCR) or PDF. Optional dependencies; raises OcrUnavailable with instructions."""
    p = path.lower()
    if p.endswith((".png", ".jpg", ".jpeg", ".webp")):
        try:
            import pytesseract
            from PIL import Image
            return pytesseract.image_to_string(Image.open(path))
        except ImportError as e:
            raise OcrUnavailable("Image import needs: pip install pytesseract pillow, plus the Tesseract OCR program installed on the server.") from e
    if p.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
        except ImportError as e:
            raise OcrUnavailable("PDF import needs: pip install pypdf (scanned PDFs also need OCR).") from e
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _num(s: str) -> float:
    s = s.lower().replace(",", "").replace("$", "").strip()
    mult = 1000.0 if s.endswith("k") else 1_000_000.0 if s.endswith("m") else 1.0
    return float(s.rstrip("km")) * mult


@dataclass
class Extraction:
    fields: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, str] = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _find_all(text: str, pattern: str) -> List[re.Match]:
    return list(re.finditer(pattern, text, re.I))


def parse_rules(text: str) -> Extraction:
    ex = Extraction()
    t = " ".join(text.split())

    def put(key, value, m):
        if key in ex.fields and ex.fields[key] != value:
            ex.conflicts.append(f"{key}: found both {ex.fields[key]} and {value}")
            return
        ex.fields[key] = value
        ex.evidence[key] = m.group(0)[:120]

    for m in _find_all(t, r"(?:account size|starting balance|initial balance|challenge|account)\D{0,12}\$?\s*([\d,.]+\s*[km]?)\b"):
        try:
            v = _num(m.group(1))
            if v >= 100:
                put("starting_balance", v, m)
        except ValueError:
            pass
    for m in _find_all(t, r"\$?\s*([\d,.]+\s*k)\s+(?:account|challenge)"):
        put("starting_balance", _num(m.group(1)), m)
    for m in _find_all(t, r"profit target\D{0,15}([\d.]+)\s*%|([\d.]+)\s*%\s*profit target"):
        put("profit_target_percent", float(m.group(1) or m.group(2)), m)
    for m in _find_all(t, r"(?:max(?:imum)?\.? )?daily (?:loss|drawdown)(?: limit)?\D{0,15}([\d.]+)\s*%|([\d.]+)\s*%\s*daily (?:loss|drawdown)"):
        put("daily_loss_limit_percent", float(m.group(1) or m.group(2)), m)
    for m in _find_all(t, r"(?:max(?:imum)?\.? (?:overall )?(?:loss|drawdown)|overall (?:loss|drawdown)(?: limit)?|total drawdown)\D{0,15}([\d.]+)\s*%|([\d.]+)\s*%\s*(?:max(?:imum)? )?(?:overall |total )?drawdown"):
        put("max_drawdown_percent", float(m.group(1) or m.group(2)), m)
    low = t.lower()
    trailing, static = "trailing" in low, bool(re.search(r"\b(static|fixed|absolute)\b", low))
    if trailing and static:
        ex.conflicts.append("drawdown_type: both 'trailing' and 'static/fixed' appear")
    elif trailing:
        ex.fields["drawdown_type"] = "trailing"
    elif static:
        ex.fields["drawdown_type"] = "static"
    eq, bal = "equity" in low, bool(re.search(r"balance[- ]based|based on (?:your )?(?:account )?balance|closed (?:trades|positions) only", low))
    if eq and bal:
        ex.conflicts.append("drawdown_basis: both equity and balance wording appear")
    elif bal:
        ex.fields["drawdown_basis"] = "balance"
    elif re.search(r"equity[- ]based|including (?:open|floating)|floating (?:p/l|pnl|profit|loss)|equity (?:drop|falls?|below)", low):
        ex.fields["drawdown_basis"] = "equity"
    if re.search(r"including (?:open|floating)|floating (?:p/l|pnl|profit|loss) (?:is |are )?(?:counted|included)", low):
        ex.fields["count_floating_pl"] = True
    elif re.search(r"(?:excluding|not including|without) (?:open|floating)", low):
        ex.fields["count_floating_pl"] = False
    m = re.search(r"(?:reset|resets|new day|day starts?|daily)\D{0,25}?(\d{1,2}):(\d{2})\s*(?:\(?\s*([A-Za-z/_]+)(?:\s*([+-]\d{1,2}))?\)?)?", t, re.I)
    if m and re.search(r"reset|resets|new day|day starts?|daily (?:loss|drawdown)? ?(?:resets?|time)", low):
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh <= 23 and mm <= 59:
            ex.fields["daily_reset_time"] = f"{hh:02d}:{mm:02d}"
            ex.evidence["daily_reset_time"] = m.group(0)[:120]
            tz = (m.group(3) or "").upper()
            if tz in TZ_MAP and not m.group(4):
                ex.fields["daily_reset_timezone"] = TZ_MAP[tz]
            elif tz:
                ex.notes.append(f"Reset timezone '{tz}{m.group(4) or ''}' is ambiguous or observes daylight saving: please enter an IANA name such as Africa/Lagos or UTC.")
    for m in _find_all(t, r"max(?:imum)?(?: position| lot)? size\D{0,12}([\d.]+)\s*lots?|max(?:imum)?\D{0,6}([\d.]+)\s*lots?"):
        put("max_lot_size", float(m.group(1) or m.group(2)), m)
    m = re.search(r"restricted (?:instruments?|symbols?|pairs?)\s*[:\-]\s*([A-Za-z0-9,/ _.]+)", t, re.I)
    if m:
        ex.fields["restricted_symbols"] = [s.strip().strip('.;').upper() for s in re.split(r"[,/]", m.group(1)) if s.strip().strip('.;')]
    m = re.search(r"(?:no|not|prohibit\w*)[^.]{0,40}(?:trad\w+)[^.]{0,40}(\d+)\s*(?:min\w*)\s*(?:before|prior|after)[^.]{0,40}news", t, re.I)
    if m:
        ex.fields["news_restricted"] = True
        ex.fields["news_window_min"] = int(m.group(1))
        ex.evidence["news_restricted"] = m.group(0)[:120]
    elif re.search(r"news trading (?:is )?(?:allowed|permitted)", low):
        ex.fields["news_restricted"] = False
    if re.search(r"(?:no|not|cannot|must not)[^.]{0,30}(?:weekend|overnight)|close[^.]{0,30}before (?:the )?weekend", low):
        ex.fields["allow_weekend"] = False
        ex.notes.append("Weekend/overnight holding rule found: the bot's scalp baskets close within minutes, but this is not enforced by a hard check yet.")
    m = re.search(r"minimum(?: of)? (\d+) (?:trading )?days", low)
    if m:
        ex.fields["min_trading_days"] = int(m.group(1))
        ex.notes.append(NOT_ENFORCED["min_trading_days"].capitalize() + " noted but not enforced by the bot.")
    if "consistency" in low:
        ex.notes.append("A consistency rule is mentioned: " + NOT_ENFORCED["consistency"] + " is NOT enforced by the bot; review it manually.")
    ex.unresolved = [k for k in REQUIRED if k not in ex.fields]
    return ex


@dataclass
class ProfileDraft:
    name: str
    extraction: Extraction
    values: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.values = dict(self.extraction.fields)

    @property
    def unresolved(self) -> List[str]:
        return [k for k in REQUIRED if self.values.get(k) in (None, "")] + \
               [f"conflict: {c}" for c in self.extraction.conflicts if c.split(":")[0] not in self.values]

    def set_field(self, key: str, raw: str) -> str:
        """Edit one value. Returns '' on success or an error message."""
        if key not in PropProfile.model_fields or key in ("name", "confirmed", "unresolved_fields"):
            return f"unknown field '{key}'"
        probe = {"name": self.name, "starting_balance": self.values.get("starting_balance", 1.0)}
        ann = PropProfile.model_fields[key].annotation
        text = raw.strip()
        try:
            if text.lower() in ("none", "null", "off") and key not in ("count_floating_pl", "news_restricted"):
                value = None
            elif "bool" in str(ann):
                value = text.lower() in ("true", "yes", "on", "1")
            elif "float" in str(ann):
                value = _num(text.rstrip("%"))
            elif "int" in str(ann):
                value = int(float(text))
            elif "List" in str(ann):
                value = [x.strip().upper() for x in text.split(",") if x.strip()]
            else:
                value = text
            if key == "daily_reset_timezone":
                try:
                    resolve_fixed_offset(str(value))
                except TimezoneError as e:
                    return str(e)
            PropProfile(**{**probe, key: value})                       # validate against the real model
        except Exception as e:
            return f"invalid value for {key}: {e}"
        self.values[key] = value
        return ""

    def to_profile(self) -> PropProfile:
        """A CONFIRMED profile, only when nothing required is missing or conflicting."""
        if self.unresolved:
            raise ValueError("unresolved: " + ", ".join(self.unresolved))
        v = {k: x for k, x in self.values.items() if k in PropProfile.model_fields and x is not None}
        v.setdefault("starting_balance", 0)
        return PropProfile(name=self.name, confirmed=True, unresolved_fields=[], **v)

    def summary(self) -> str:
        lines = [f"Draft '{self.name}':"]
        for k, val in sorted(self.values.items()):
            ev = self.extraction.evidence.get(k)
            lines.append(f"  {k} = {val}" + (f"   (from: \"{ev}\")" if ev else ""))
        if self.unresolved:
            lines.append("MISSING / UNCLEAR (set with /ruleset <field> <value>): " + ", ".join(self.unresolved))
        lines += ["Note: " + n for n in self.extraction.notes]
        lines.append("Review everything. Following this profile does not guarantee compliance with the firm's full terms.")
        return "\n".join(lines)
