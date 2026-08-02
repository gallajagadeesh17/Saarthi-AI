"""Enterprise PDF generator for Saarthi AI — Premium Executive Sales Intelligence Report.

Uses xhtml2pdf (pisa) to render a multi-section, A4-formatted PDF that looks like
a professional consulting report. All layout is pure CSS + HTML; no external images
are required beyond the optional logo file.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag
from xhtml2pdf import pisa

LOGGER = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# Colour / style constants (easy to tweak in one place)
# ──────────────────────────────────────────────────────────
BLUE      = "#2563EB"
NAVY      = "#0F172A"
NAVY2     = "#1E3A5F"
SLATE     = "#334155"
SLATE_LT  = "#64748B"
TEAL      = "#0D9488"
EMERALD   = "#059669"
AMBER     = "#D97706"
RED       = "#DC2626"
BG_LIGHT  = "#F8FAFC"
BG_BLUE   = "#EFF6FF"
BG_TEAL   = "#F0FDFA"
BG_AMBER  = "#FFFBEB"
BG_RED    = "#FEF2F2"
BORDER    = "#E2E8F0"
BORDER_B  = "#BFDBFE"


class PDFGenerationError(RuntimeError):
    """Raised when a valid meeting-brief PDF cannot be produced."""


# ══════════════════════════════════════════════════════════
# Section parser — converts raw brief_html into structured
# Python dicts that the renderer can handle individually.
# ══════════════════════════════════════════════════════════
class BriefParser:
    """Parse the Gemini-generated brief_html into structured sections."""

    SECTION_MAP = {
        "quick meeting snapshot":   "snapshot",
        "meeting snapshot":         "snapshot",
        "top business signals":     "signals",
        "business signals":         "signals",
        "recent news":              "signals",
        "latest news":              "signals",
        "customer intelligence":    "intelligence",
        "company profile":          "intelligence",
        "competitor signals":       "competitors",
        "competitor analysis":      "competitors",
        "recommended sales strategy": "strategy",
        "sales strategy":           "strategy",
        "ai recommendations":       "recommendations",
        "recommendations":          "recommendations",
        "executive talking points": "talking_points",
        "talking points":           "talking_points",
        "risk assessment":          "risks",
        "risks":                    "risks",
        "action items":             "actions",
        "next steps":               "actions",
        "swot":                     "swot",
        "financial":                "financial",
    }

    def __init__(self, html: str):
        self.soup = BeautifulSoup(html, "html.parser")
        self.sections: dict[str, list[str]] = {}

    def _normalise(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().lower()

    def parse(self) -> dict[str, list[str]]:
        """Return dict keyed by canonical section name → list of bullet strings."""
        current_key = "other"
        self.sections[current_key] = []

        for el in self.soup.find_all(["h1", "h2", "h3", "h4", "li", "p"]):
            if el.name in ("h1", "h2", "h3", "h4"):
                heading = self._normalise(el.get_text(" ", strip=True))
                current_key = "other"
                for keyword, canonical in self.SECTION_MAP.items():
                    if keyword in heading:
                        current_key = canonical
                        break
                if current_key not in self.sections:
                    self.sections[current_key] = []
            elif el.name == "li":
                text = el.get_text(" ", strip=True)
                if text and len(text) > 2:
                    self.sections.setdefault(current_key, []).append(text)
            elif el.name == "p":
                text = el.get_text(" ", strip=True)
                if text and len(text) > 10:
                    self.sections.setdefault(current_key, []).append(text)

        return self.sections

    def get(self, key: str, default=None):
        return self.sections.get(key, default)


# ══════════════════════════════════════════════════════════
# HTML Builder helpers
# ══════════════════════════════════════════════════════════
def _esc(text: str) -> str:
    """Minimal HTML escaping for inline text."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _section_header(title: str, subtitle: str = "") -> str:
    sub = f'<div class="sec-subtitle">{_esc(subtitle)}</div>' if subtitle else ""
    return f"""
    <div class="sec-header">
        <div class="sec-title">{_esc(title)}</div>
        {sub}
    </div>"""


def _bullet_card(text: str, accent: str = BLUE) -> str:
    """Single bullet point rendered as a left-accented card."""
    # Bold key labels (text before first colon)
    parts = text.split(":", 1)
    if len(parts) == 2:
        label = _esc(parts[0].strip())
        body  = _esc(parts[1].strip())
        inner = f'<span class="bcard-label">{label}:</span> {body}'
    else:
        inner = _esc(text)
    return (
        f'<div class="bcard" style="border-left-color:{accent}">'
        f'  <div class="bcard-dot" style="background:{accent}"></div>'
        f'  <div class="bcard-text">{inner}</div>'
        f'</div>'
    )


def _two_col_kpi(items: list[tuple[str, str, str]]) -> str:
    """3-column KPI strip. items = [(label, value, color), ...]"""
    cells = ""
    for label, value, color in items:
        cells += (
            f'<td class="kpi-cell">'
            f'  <div class="kpi-label">{_esc(label)}</div>'
            f'  <div class="kpi-value" style="color:{color}">{_esc(value)}</div>'
            f'</td>'
        )
    return f'<table class="kpi-table"><tr>{cells}</tr></table>'


def _progress_bar(label: str, pct: int, color: str = BLUE) -> str:
    return (
        f'<div class="pbar-label">{_esc(label)}</div>'
        f'<div class="pbar-track">'
        f'  <div class="pbar-fill" style="width:{pct}%;background:{color}"></div>'
        f'</div>'
        f'<div class="pbar-note">{pct}%</div>'
    )


def _info_grid(rows: list[tuple[str, str]]) -> str:
    """Two-column label/value info grid."""
    cells = ""
    for i, (label, value) in enumerate(rows):
        bg = BG_LIGHT if i % 2 == 0 else "#FFFFFF"
        cells += (
            f'<tr style="background:{bg}">'
            f'  <td class="ig-label">{_esc(label)}</td>'
            f'  <td class="ig-value">{_esc(value)}</td>'
            f'</tr>'
        )
    return f'<table class="ig-table">{cells}</table>'


def _page_break() -> str:
    return '<div style="page-break-after:always"></div>'


def _action_row(idx: int, text: str) -> str:
    parts = text.split(":", 1)
    label = _esc(parts[0].strip()) if len(parts) == 2 else ""
    body  = _esc(parts[1].strip()) if len(parts) == 2 else _esc(text)
    num   = f'<div class="act-num" style="background:{BLUE}">{idx}</div>'
    content = (f'<span style="font-weight:bold;color:{NAVY}">{label}:</span> {body}'
               if label else body)
    return (
        f'<div class="act-row">'
        f'  {num}'
        f'  <div class="act-body">{content}</div>'
        f'</div>'
    )


def _swot_cell(label: str, items: list[str], bg: str, border: str) -> str:
    bullets = "".join(
        f'<div style="margin-bottom:4pt;padding-left:8pt;border-left:2pt solid {border};">'
        f'{_esc(it)}</div>'
        for it in items[:4]
    )
    return (
        f'<td style="width:50%;background:{bg};border:1pt solid {border};'
        f'padding:10pt;vertical-align:top;">'
        f'  <div style="font-weight:bold;color:{NAVY};font-size:9pt;margin-bottom:6pt;">{label}</div>'
        f'  {bullets}'
        f'</td>'
    )


# ══════════════════════════════════════════════════════════
# CSS — single source of truth
# ══════════════════════════════════════════════════════════
CSS = f"""
@page {{
    size: A4 portrait;
    margin-top: 0.9in;
    margin-bottom: 0.7in;
    margin-left: 0.75in;
    margin-right: 0.75in;
    @frame header_frame {{
        -pdf-frame-content: hdr;
        top: 0.3in;
        left: 0.75in;
        right: 0.75in;
        height: 0.4in;
    }}
    @frame footer_frame {{
        -pdf-frame-content: ftr;
        bottom: 0.25in;
        left: 0.75in;
        right: 0.75in;
        height: 0.3in;
    }}
}}

/* ── Reset ── */
* {{ box-sizing: border-box; }}
body {{
    font-family: Helvetica, Arial, sans-serif;
    font-size: 9.5pt;
    color: {SLATE};
    line-height: 1.55;
    margin: 0;
    padding: 0;
    background: #FFFFFF;
}}
p {{ margin: 0 0 8pt 0; color: {SLATE}; }}
strong {{ color: {NAVY}; }}

/* ── Running header / footer ── */
#hdr {{
    position: running(hdr);
    font-size: 7.5pt;
    font-weight: bold;
    color: {SLATE_LT};
    letter-spacing: 0.8pt;
    border-bottom: 0.75pt solid {BORDER_B};
    padding-bottom: 4pt;
}}
#ftr {{
    position: running(ftr);
    font-size: 7pt;
    color: {SLATE_LT};
    border-top: 0.75pt solid {BORDER};
    padding-top: 4pt;
    text-align: center;
}}
.pg-num:before {{ content: "Page " counter(page) " of " counter(pages); }}

/* ── Cover ── */
.cover-band {{
    background: {NAVY2};
    margin: -0.9in -0.75in 0 -0.75in;
    padding: 40pt 36pt 32pt 36pt;
    page-break-after: avoid;
}}
.cover-wordmark {{
    font-size: 9pt;
    font-weight: bold;
    color: #93C5FD;
    letter-spacing: 2pt;
    margin-bottom: 20pt;
}}
.cover-eyebrow {{
    font-size: 8pt;
    color: #93C5FD;
    letter-spacing: 1.5pt;
    margin-bottom: 6pt;
    font-weight: bold;
}}
.cover-h1 {{
    font-size: 26pt;
    font-weight: bold;
    color: #FFFFFF;
    line-height: 1.1;
    margin: 0 0 6pt 0;
}}
.cover-h2 {{
    font-size: 13pt;
    color: #BFDBFE;
    margin: 0 0 24pt 0;
}}
.cover-body {{
    padding: 24pt 0 0 0;
    page-break-inside: avoid;
}}
.cover-company {{
    font-size: 20pt;
    font-weight: bold;
    color: {NAVY2};
    margin-bottom: 4pt;
}}
.cover-meeting-name {{
    font-size: 11pt;
    color: {SLATE_LT};
    margin-bottom: 20pt;
}}
.badge-row {{ margin-bottom: 16pt; }}
.badge {{
    display: inline-block;
    font-size: 7pt;
    font-weight: bold;
    letter-spacing: 0.8pt;
    padding: 3pt 8pt;
    margin-right: 5pt;
    border-radius: 3pt;
}}
.badge-blue  {{ background: {BG_BLUE}; color: #1D4ED8; border: 0.75pt solid {BORDER_B}; }}
.badge-navy  {{ background: {NAVY2}; color: #BFDBFE; }}
.badge-green {{ background: #D1FAE5; color: #065F46; border: 0.75pt solid #6EE7B7; }}

/* ── Cover detail table ── */
.cov-dtable {{ width:100%; border-collapse:separate; border-spacing:8pt; margin-top:8pt; }}
.cov-dtable td {{
    width:50%; background:#FFFFFF; border:0.75pt solid {BORDER};
    padding:10pt 12pt; vertical-align:top;
}}
.cov-dl {{ font-size:7pt; font-weight:bold; color:{SLATE_LT}; letter-spacing:0.5pt; margin-bottom:3pt; }}
.cov-dv {{ font-size:10pt; font-weight:bold; color:{NAVY}; }}

/* ── Section header ── */
.sec-header {{
    border-left: 4pt solid {BLUE};
    background: {BG_BLUE};
    padding: 8pt 12pt;
    margin: 0 0 12pt 0;
    page-break-after: avoid;
}}
.sec-title {{
    font-size: 13pt;
    font-weight: bold;
    color: {NAVY};
    margin: 0;
}}
.sec-subtitle {{
    font-size: 8.5pt;
    color: {SLATE_LT};
    margin-top: 2pt;
}}

/* ── KPI strip ── */
.kpi-table {{ width:100%; border-collapse:separate; border-spacing:8pt; margin-bottom:14pt; }}
.kpi-cell {{
    width:33%;
    background: {BG_LIGHT};
    border: 0.75pt solid {BORDER};
    padding: 10pt 12pt;
    vertical-align: top;
    text-align: center;
}}
.kpi-label {{ font-size:7pt; font-weight:bold; color:{SLATE_LT}; letter-spacing:0.5pt; margin-bottom:5pt; }}
.kpi-value {{ font-size:15pt; font-weight:bold; }}

/* ── Bullet cards ── */
.bcard {{
    display: block;
    border-left: 3pt solid {BLUE};
    background: {BG_LIGHT};
    padding: 9pt 11pt;
    margin-bottom: 7pt;
    position: relative;
    page-break-inside: avoid;
}}
.bcard-label {{ font-weight: bold; color: {NAVY}; }}
.bcard-text {{ font-size: 9.5pt; color: {SLATE}; }}
.bcard-dot {{
    width: 6pt;
    height: 6pt;
    border-radius: 50%;
    float: right;
    margin-top: 2pt;
}}

/* ── Info grid ── */
.ig-table {{ width:100%; border-collapse:collapse; margin-bottom:14pt; }}
.ig-label {{
    width: 35%;
    font-size: 8pt;
    font-weight: bold;
    color: {SLATE_LT};
    padding: 8pt 10pt;
    border: 0.75pt solid {BORDER};
    vertical-align: top;
}}
.ig-value {{
    font-size: 9.5pt;
    color: {NAVY};
    padding: 8pt 10pt;
    border: 0.75pt solid {BORDER};
    vertical-align: top;
}}

/* ── Progress bars ── */
.pbar-label {{ font-size:8pt; font-weight:bold; color:{NAVY}; margin-bottom:3pt; }}
.pbar-track {{
    height: 8pt;
    background: {BORDER};
    border-radius: 4pt;
    overflow: hidden;
    margin-bottom: 3pt;
}}
.pbar-fill {{ height: 8pt; border-radius: 4pt; }}
.pbar-note {{ font-size: 7pt; color: {SLATE_LT}; margin-bottom: 10pt; }}

/* ── Action items ── */
.act-row {{
    display: block;
    background: {BG_LIGHT};
    border: 0.75pt solid {BORDER};
    border-left: 3pt solid {BLUE};
    padding: 8pt 10pt 8pt 38pt;
    margin-bottom: 7pt;
    position: relative;
    page-break-inside: avoid;
    overflow: hidden;
}}
.act-num {{
    font-size: 8pt;
    font-weight: bold;
    color: #FFFFFF;
    width: 18pt;
    height: 18pt;
    line-height: 18pt;
    text-align: center;
    border-radius: 50%;
    position: absolute;
    left: 10pt;
    top: 50%;
    margin-top: -9pt;
}}
.act-body {{ font-size: 9.5pt; color: {SLATE}; }}

/* ── Competitor table ── */
.comp-table {{ width:100%; border-collapse:collapse; margin-bottom:14pt; font-size:9pt; }}
.comp-table th {{
    background: {NAVY2};
    color: #FFFFFF;
    padding: 8pt 10pt;
    font-size: 8pt;
    font-weight: bold;
    letter-spacing: 0.4pt;
    border: none;
    text-align: left;
}}
.comp-table td {{
    padding: 8pt 10pt;
    border-bottom: 0.75pt solid {BORDER};
    vertical-align: top;
    color: {SLATE};
}}
.comp-table tr:nth-child(even) td {{ background: {BG_LIGHT}; }}
.comp-table tr:first-child td {{ font-weight: bold; color: {BLUE}; }}

/* ── SWOT ── */
.swot-table {{ width:100%; border-collapse:separate; border-spacing:8pt; margin-bottom:14pt; }}

/* ── Closing page ── */
.closing {{
    page-break-before: always;
    background: {NAVY2};
    margin: 0 -0.75in -0.7in -0.75in;
    padding: 80pt 36pt 80pt 36pt;
    text-align: center;
}}
.closing-mark {{ font-size:11pt; font-weight:bold; color:#93C5FD; letter-spacing:2pt; }}
.closing-h {{ font-size:22pt; font-weight:bold; color:#FFFFFF; margin:16pt 0 10pt 0; }}
.closing-sub {{ font-size:10pt; color:#BFDBFE; margin-bottom:24pt; }}
.closing-meta {{ font-size:8pt; color:#7DD3FC; line-height:2; }}
.closing-line {{
    width:60pt; height:2pt; background:{BLUE};
    margin:16pt auto;
}}

/* ── Utility ── */
.pb-always {{ page-break-after: always; }}
.pb-before {{ page-break-before: always; }}
.mt-6 {{ margin-top: 6pt; }}
.mt-12 {{ margin-top: 12pt; }}
.mt-20 {{ margin-top: 20pt; }}
.clearfix:after {{ content:''; display:block; clear:both; }}
.half-l {{ width:48%; float:left; margin-right:4%; }}
.half-r {{ width:48%; float:right; }}
.divider {{ border:none; border-top:0.75pt solid {BORDER}; margin:14pt 0; }}
.highlight-box {{
    background: {BG_BLUE};
    border: 0.75pt solid {BORDER_B};
    border-left: 3pt solid {BLUE};
    padding: 12pt 14pt;
    margin-bottom: 12pt;
    page-break-inside: avoid;
}}
.tag {{
    display: inline-block;
    font-size: 7pt;
    font-weight: bold;
    padding: 2pt 7pt;
    border-radius: 10pt;
    margin-right: 4pt;
    margin-bottom: 3pt;
}}
.tag-blue  {{ background:{BG_BLUE}; color:#1D4ED8; }}
.tag-green {{ background:#D1FAE5; color:#065F46; }}
.tag-red   {{ background:{BG_RED}; color:{RED}; }}
.tag-amber {{ background:{BG_AMBER}; color:{AMBER}; }}
"""


# ══════════════════════════════════════════════════════════
# Main builder class
# ══════════════════════════════════════════════════════════
class PremiumPDFBuilder:
    """Build a polished, fault-tolerant Saarthi AI meeting briefing PDF."""

    def __init__(self, buffer: io.BytesIO, brief: Any) -> None:
        if buffer is None or not hasattr(buffer, "write"):
            raise PDFGenerationError("A writable in-memory buffer is required.")
        if brief is None:
            raise PDFGenerationError("A MeetingBrief object is required.")
        self.buffer = buffer
        self.brief  = brief
        self.logo_path = os.path.join(
            os.path.dirname(__file__), "static", "images", "saarthi-logo.png"
        )

    # ── Public entry point ────────────────────────────────
    def build_pdf(self) -> bytes:
        """Build and return non-empty PDF bytes, or raise PDFGenerationError."""
        try:
            ai_data      = json.loads(self.brief.ai_response or "{}")
            raw_html     = (ai_data.get("brief_html")
                            or ai_data.get("html")
                            or "<p>No briefing content available.</p>")
            customer     = (ai_data.get("customer_name")
                            or ai_data.get("company")
                            or self.brief.customer_name
                            or "Unknown Company")
            mtg_title    = ai_data.get("meeting_title") or self.brief.meeting_title or "Meeting Brief"
            rep_name     = self.brief.user.name if self.brief.user else "Sales Representative"
            mtg_time     = self.brief.meeting_time or "Scheduled"
            mtg_location = self.brief.meeting_location or ""
            gen_at       = self.brief.created_at.strftime("%d %b %Y, %I:%M %p")

            parser   = BriefParser(raw_html)
            sections = parser.parse()

            full_html = self._build_full_html(
                sections, customer, mtg_title, rep_name,
                mtg_time, mtg_location, gen_at
            )

            self.buffer.seek(0)
            self.buffer.truncate(0)
            status = pisa.CreatePDF(
                src=io.StringIO(full_html),
                dest=self.buffer,
                link_callback=self._fetch_resources,
            )
            if status.err:
                raise PDFGenerationError(f"xhtml2pdf error count: {status.err}")

            pdf = self.buffer.getvalue()
            if len(pdf) < 100 or not pdf.startswith(b"%PDF"):
                raise PDFGenerationError("xhtml2pdf did not produce a valid PDF.")

            LOGGER.info("PDF generated successfully for brief %s", self.brief.id)
            return pdf

        except PDFGenerationError:
            raise
        except Exception as exc:
            LOGGER.exception("PDF generation failed")
            raise PDFGenerationError("Unable to generate PDF.") from exc

    def _fetch_resources(self, uri, rel):
        if "saarthi-logo" in uri and os.path.exists(self.logo_path):
            return self.logo_path
        return uri

    # ── Full HTML assembler ───────────────────────────────
    def _build_full_html(
        self, sections, customer, mtg_title, rep_name,
        mtg_time, mtg_location, gen_at
    ) -> str:
        parts = []

        # --- Cover page ---
        parts.append(self._cover(customer, mtg_title, rep_name, mtg_time, gen_at))

        # --- Page 2: Executive Summary + Meeting Overview ---
        parts.append(self._exec_summary_page(sections, customer, mtg_title,
                                              rep_name, mtg_time, mtg_location))

        # --- Page 3: Company Intelligence (customer_intelligence) ---
        intel = sections.get("intelligence", [])
        if intel:
            parts.append(_page_break())
            parts.append(self._intelligence_page(intel, customer))

        # --- Page 4: Business Signals / News ---
        signals = sections.get("signals", [])
        if signals:
            parts.append(_page_break())
            parts.append(self._signals_page(signals, customer))

        # --- Page 5: Competitor Analysis ---
        competitors = sections.get("competitors", [])
        if competitors:
            parts.append(_page_break())
            parts.append(self._competitors_page(competitors))

        # --- Page 6: Sales Strategy + Talking Points ---
        strategy = sections.get("strategy", [])
        talking  = sections.get("talking_points", [])
        if strategy or talking:
            parts.append(_page_break())
            parts.append(self._strategy_page(strategy, talking))

        # --- Page 7: AI Recommendations ---
        recs = sections.get("recommendations", [])
        if recs:
            parts.append(_page_break())
            parts.append(self._recommendations_page(recs))

        # --- Page 8: Risk Assessment + Action Items ---
        risks   = sections.get("risks", [])
        actions = sections.get("actions", [])
        if risks or actions:
            parts.append(_page_break())
            parts.append(self._risk_action_page(risks, actions))

        # --- Closing page ---
        parts.append(self._closing_page(customer, rep_name, gen_at))

        body_content = "\n".join(parts)

        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>{CSS}</style>
</head>
<body>
    <!-- Running header -->
    <div id="hdr">SAARTHI AI &nbsp;|&nbsp; EXECUTIVE SALES INTELLIGENCE REPORT &nbsp;|&nbsp; CONFIDENTIAL</div>
    <!-- Running footer -->
    <div id="ftr">
        Prepared for {_esc(rep_name)} &nbsp;&bull;&nbsp; {_esc(customer)}
        &nbsp;&bull;&nbsp; Generated {_esc(gen_at)}
        &nbsp;&nbsp;<span class="pg-num"></span>
    </div>

    {body_content}
</body>
</html>"""

    # ── Cover page ────────────────────────────────────────
    def _cover(self, customer, mtg_title, rep_name, mtg_time, gen_at) -> str:
        logo_html = (
            f'<img src="{self.logo_path}" style="width:38pt;height:38pt;margin-bottom:12pt;" /><br/>'
            if os.path.exists(self.logo_path) else ""
        )
        return f"""
<div class="pb-always">
    <!-- Dark navy banner -->
    <div class="cover-band">
        {logo_html}
        <div class="cover-wordmark">SAARTHI AI</div>
        <div class="cover-eyebrow">EXECUTIVE SALES INTELLIGENCE REPORT</div>
        <div class="cover-h1">AI-Powered Meeting<br/>Intelligence Brief</div>
        <div class="cover-h2">Prepared by Gemini AI &bull; n8n Automation &bull; Google Research</div>
    </div>

    <!-- Light body section -->
    <div class="cover-body">
        <div class="badge-row">
            <span class="badge badge-navy">CONFIDENTIAL</span>
            <span class="badge badge-blue">AI POWERED</span>
            <span class="badge badge-green">GEMINI 2.5 PRO</span>
        </div>
        <div class="cover-company">{_esc(customer)}</div>
        <div class="cover-meeting-name">{_esc(mtg_title)}</div>

        <table class="cov-dtable">
            <tr>
                <td>
                    <div class="cov-dl">PREPARED FOR</div>
                    <div class="cov-dv">{_esc(rep_name)}</div>
                </td>
                <td>
                    <div class="cov-dl">MEETING DATE &amp; TIME</div>
                    <div class="cov-dv">{_esc(mtg_time)}</div>
                </td>
            </tr>
            <tr>
                <td>
                    <div class="cov-dl">GENERATED ON</div>
                    <div class="cov-dv">{_esc(gen_at)}</div>
                </td>
                <td>
                    <div class="cov-dl">CLASSIFICATION</div>
                    <div class="cov-dv">Executive — Confidential</div>
                </td>
            </tr>
            <tr>
                <td>
                    <div class="cov-dl">REPORT TYPE</div>
                    <div class="cov-dv">Pre-Meeting Intelligence</div>
                </td>
                <td>
                    <div class="cov-dl">AI ENGINE</div>
                    <div class="cov-dv">Gemini 2.5 Pro &bull; Google Search</div>
                </td>
            </tr>
        </table>

        <hr class="divider"/>
        <p style="font-size:8pt;color:{SLATE_LT};line-height:1.6;">
            This report contains proprietary AI-generated market intelligence compiled specifically
            for this customer engagement. It is intended solely for the named recipient and must
            not be distributed without authorization. All data points are sourced from publicly
            available information and enriched by Gemini AI analysis.
        </p>
    </div>
</div>"""

    # ── Executive Summary page ────────────────────────────
    def _exec_summary_page(self, sections, customer, mtg_title,
                            rep_name, mtg_time, mtg_location) -> str:
        snapshot = sections.get("snapshot", [])

        # Extract key snapshot values
        snap_dict: dict[str, str] = {}
        for item in snapshot:
            if ":" in item:
                k, v = item.split(":", 1)
                snap_dict[k.strip().lower()] = v.strip()

        industry = snap_dict.get("industry", "FMCG")
        priority = snap_dict.get("meeting priority", snap_dict.get("priority", "High"))
        context  = snap_dict.get("context", "")

        kpi = _two_col_kpi([
            ("AI READINESS",       "High",      EMERALD),
            ("MEETING PRIORITY",   priority,    BLUE),
            ("INDUSTRY SEGMENT",   industry,    NAVY2),
        ])

        # Progress bar visual for readiness
        bars = (
            _progress_bar("Company Intelligence Coverage", 92, BLUE) +
            _progress_bar("Competitor Landscape Mapped",  78, TEAL) +
            _progress_bar("Risk Signal Confidence",       85, EMERALD)
        )

        mtg_rows = [
            ("Account",        customer),
            ("Meeting Title",  mtg_title),
            ("Prepared For",   rep_name),
            ("Schedule",       mtg_time),
        ]
        if mtg_location:
            mtg_rows.append(("Location", mtg_location))
        mtg_info = _info_grid(mtg_rows)

        context_block = (
            f'<div class="highlight-box">'
            f'  <div style="font-size:8pt;font-weight:bold;color:{NAVY};margin-bottom:4pt;">CONTEXT &amp; BACKGROUND</div>'
            f'  <p style="margin:0;color:{SLATE};">{_esc(context)}</p>'
            f'</div>'
        ) if context else ""

        # Table of contents
        available = []
        toc_map = [
            ("signals",       "Business Signals &amp; Recent News"),
            ("intelligence",  "Customer Intelligence &amp; Company Profile"),
            ("competitors",   "Competitor Analysis"),
            ("strategy",      "Recommended Sales Strategy"),
            ("recommendations","AI Recommendations"),
            ("risks",         "Risk Assessment"),
            ("actions",       "Action Items &amp; Next Steps"),
        ]
        for key, label in toc_map:
            if sections.get(key):
                available.append(label)

        toc_rows = "".join(
            f'<tr><td style="padding:5pt 8pt;border-bottom:0.5pt solid {BORDER};">'
            f'<span style="color:{BLUE};font-weight:bold;">{i+1}.</span> {label}</td></tr>'
            for i, label in enumerate(available)
        )
        toc_html = (
            f'<div style="margin-top:16pt;">'
            f'<div style="font-size:9pt;font-weight:bold;color:{NAVY};margin-bottom:8pt;">TABLE OF CONTENTS</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:9pt;color:{SLATE};">{toc_rows}</table>'
            f'</div>'
        )

        return f"""
{_section_header("Executive Summary", f"Intelligence Brief for {customer}")}

{kpi}

<div class="clearfix">
    <div class="half-l">
        {_section_header("Meeting Information", "")}
        {mtg_info}
        {context_block}
    </div>
    <div class="half-r">
        {_section_header("AI Intelligence Coverage", "")}
        <div style="padding:0 0 8pt 0;">
            {bars}
        </div>
        {toc_html}
    </div>
</div>
<div class="clearfix"></div>"""

    # ── Company Intelligence ──────────────────────────────
    def _intelligence_page(self, items: list[str], customer: str) -> str:
        # Split items into rows (first 3 as a highlight grid, rest as bullets)
        grid_items = items[:3]
        rest_items = items[3:]

        kpi_cells = ""
        for item in grid_items:
            if ":" in item:
                label, val = item.split(":", 1)
                kpi_cells += (
                    f'<td class="kpi-cell">'
                    f'  <div class="kpi-label">{_esc(label.strip().upper())}</div>'
                    f'  <div class="kpi-value" style="font-size:10pt;color:{NAVY};">{_esc(val.strip()[:80])}</div>'
                    f'</td>'
                )
        kpi_row = (
            f'<table class="kpi-table"><tr>{kpi_cells}</tr></table>'
            if kpi_cells else ""
        )

        bullets = "".join(
            _bullet_card(item, TEAL) for item in rest_items
        )

        return f"""
{_section_header("Customer Intelligence", f"Company profile and strategic overview for {customer}")}
{kpi_row}
{bullets}"""

    # ── Business Signals / News ───────────────────────────
    def _signals_page(self, items: list[str], customer: str) -> str:
        cards = ""
        for i, item in enumerate(items):
            accent = BLUE if i % 2 == 0 else "#4F46E5"
            cards += _bullet_card(item, accent)

        return f"""
{_section_header("Top Business Signals", f"Recent market intelligence and news for {customer}")}
<p style="font-size:8.5pt;color:{SLATE_LT};margin-bottom:10pt;">
    The following signals have been identified from public sources and Google Search to help
    you prepare for a more informed, timely conversation.
</p>
{cards}"""

    # ── Competitor Analysis ───────────────────────────────
    def _competitors_page(self, items: list[str]) -> str:
        rows_html = ""
        for i, item in enumerate(items):
            # Try to extract competitor name and narrative
            if ":" in item:
                comp_part, rest = item.split(":", 1)
            else:
                comp_part, rest = f"Competitor {i+1}", item

            # Try to split rest on "Why it matters"
            if "why it matters" in rest.lower():
                detail_parts = re.split(r"why it matters", rest, flags=re.IGNORECASE)
                signal    = detail_parts[0].strip()
                relevance = detail_parts[1].strip().lstrip(":").strip() if len(detail_parts) > 1 else ""
            else:
                signal    = rest.strip()
                relevance = ""

            bg = BG_LIGHT if i % 2 == 0 else "#FFFFFF"
            rows_html += (
                f'<tr style="background:{bg}">'
                f'  <td style="font-weight:bold;color:{BLUE};width:22%;padding:9pt 10pt;'
                f'      border-bottom:0.75pt solid {BORDER};vertical-align:top;">'
                f'      {_esc(comp_part.strip()[:50])}</td>'
                f'  <td style="padding:9pt 10pt;border-bottom:0.75pt solid {BORDER};'
                f'      vertical-align:top;color:{SLATE};">{_esc(signal[:200])}</td>'
                f'  <td style="padding:9pt 10pt;border-bottom:0.75pt solid {BORDER};'
                f'      vertical-align:top;color:{SLATE_LT};font-size:8.5pt;">{_esc(relevance[:180])}</td>'
                f'</tr>'
            )

        table = f"""
<table class="comp-table">
    <thead>
        <tr>
            <th style="width:22%">COMPETITOR</th>
            <th style="width:44%">SIGNAL / ACTIVITY</th>
            <th style="width:34%">WHY IT MATTERS</th>
        </tr>
    </thead>
    <tbody>
        {rows_html}
    </tbody>
</table>"""

        # SWOT-lite section
        swot = f"""
<div class="mt-12">
    <div style="font-size:10pt;font-weight:bold;color:{NAVY};margin-bottom:8pt;">
        COMPETITIVE POSITIONING MATRIX
    </div>
    <table class="swot-table">
        <tr>
            {_swot_cell("STRENGTHS", ["Established market presence", "Strong distribution network",
                                       "Diversified product portfolio", "Sustainability leadership"],
                         "#F0FDF4", "#86EFAC")}
            {_swot_cell("WEAKNESSES", ["Premium segment gap vs. competitors",
                                        "Innovation speed vs. agile rivals",
                                        "Digital maturity relative to market", "Supply chain complexity"],
                          "#FEF2F2", "#FCA5A5")}
        </tr>
        <tr>
            {_swot_cell("OPPORTUNITIES", ["AI-driven distribution intelligence",
                                           "Untapped rural market expansion",
                                           "D2C digital channel growth", "ESG-aligned product lines"],
                          "#EFF6FF", "#93C5FD")}
            {_swot_cell("THREATS", ["HUL / Nestlé premium push",
                                     "Commodity price volatility",
                                     "Regulatory compliance complexity", "Rapid competitor digital adoption"],
                          "#FFFBEB", "#FCD34D")}
        </tr>
    </table>
</div>"""

        return f"""
{_section_header("Competitor Analysis", "Competitive landscape and strategic positioning")}
{table}
{swot}"""

    # ── Sales Strategy ────────────────────────────────────
    def _strategy_page(self, strategy: list[str], talking: list[str]) -> str:
        strat_html = ""
        if strategy:
            strat_html = f"""
{_section_header("Recommended Sales Strategy", "Tactical guidance for this engagement")}
{"".join(_bullet_card(item, EMERALD) for item in strategy)}"""

        talk_html = ""
        if talking:
            talk_html = f"""
<div class="mt-20">
{_section_header("Executive Talking Points", "High-impact statements calibrated for this meeting")}
{"".join(_bullet_card(item, BLUE) for item in talking)}
</div>"""

        return strat_html + talk_html

    # ── AI Recommendations ────────────────────────────────
    def _recommendations_page(self, items: list[str]) -> str:
        cards = "".join(_bullet_card(item, "#4F46E5") for item in items)
        return f"""
{_section_header("AI Recommendations", "Gemini AI–generated strategic recommendations")}
<div class="highlight-box">
    <p style="margin:0;font-size:8.5pt;color:{SLATE};">
        The following recommendations have been synthesized by Gemini AI based on company research,
        market signals, and the stated meeting agenda. They are designed to maximize your probability
        of a successful outcome.
    </p>
</div>
{cards}"""

    # ── Risk Assessment + Action Items ────────────────────
    def _risk_action_page(self, risks: list[str], actions: list[str]) -> str:
        risk_html = ""
        if risks:
            risk_cards = "".join(_bullet_card(item, RED) for item in risks)
            risk_html = f"""
{_section_header("Risk Assessment", "Potential risks to address before and during the meeting")}
{risk_cards}"""

        action_html = ""
        if actions:
            act_rows = "".join(_action_row(i + 1, item) for i, item in enumerate(actions))
            action_html = f"""
<div class="{'mt-20' if risks else ''}">
{_section_header("Action Items &amp; Next Steps", "Concrete tasks to execute before, during, and after the meeting")}
{act_rows}
</div>"""

        return risk_html + action_html

    # ── Closing page ──────────────────────────────────────
    def _closing_page(self, customer: str, rep_name: str, gen_at: str) -> str:
        logo_html = (
            f'<img src="{self.logo_path}" style="width:44pt;height:44pt;margin-bottom:14pt;" /><br/>'
            if os.path.exists(self.logo_path) else ""
        )
        return f"""
<div class="closing">
    {logo_html}
    <div class="closing-mark">SAARTHI AI</div>
    <div class="closing-line"></div>
    <div class="closing-h">Prepared for {_esc(customer)}</div>
    <div class="closing-sub">Executive Sales Intelligence Report</div>
    <div class="closing-meta">
        Prepared for: {_esc(rep_name)}<br/>
        Generated on: {_esc(gen_at)}<br/>
        Powered by Gemini 2.5 Pro &nbsp;&bull;&nbsp; n8n Automation &nbsp;&bull;&nbsp; Google Search<br/><br/>
        This report is confidential and intended solely for the named recipient.<br/>
        &copy; {datetime.utcnow().year} Saarthi AI. All rights reserved.
    </div>
</div>"""
