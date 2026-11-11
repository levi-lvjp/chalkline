"""Quote and invoice PDFs from the immutable ledger snapshot."""

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .files import save_bytes
from .money import major


INK = (25, 39, 45)
MUTED = (74, 94, 100)
ACCENT = (16, 85, 82)
PALE = (235, 244, 242)


def _fonts(pdf) -> tuple[str, tuple[Path, ...]]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    bundled = Path(__file__).resolve().parents[2] / "fonts" / "NotoSans.ttf"
    candidates = (
        (bundled, bundled),
        (windows / "arial.ttf", windows / "arialbd.ttf"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
         Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf")),
        (Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
         Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
    )
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            pdf.add_font("Office", fname=regular)
            pdf.add_font("Office", style="B", fname=bold)
            paths = (
                windows / "Nirmala.ttc", windows / "msyh.ttc", windows / "YuGothM.ttc", windows / "malgun.ttf",
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            )
            # Load fallback fonts only when the renderer encounters text outside the main font.
            return "Office", paths
    pdf.core_fonts_encoding = "cp1252"
    return "Helvetica", ()


def _support_text(pdf, family: str, paths: tuple[Path, ...], content: str) -> None:
    if family == "Helvetica":
        try:
            content.encode("cp1252")
        except UnicodeEncodeError as exc:
            raise ValueError("Install Arial or DejaVu Sans to include this text in the PDF.") from exc
        return
    characters = {ord(char) for char in content if ord(char) >= 32}
    missing = set()
    for style in ("", "B"):
        pdf.set_font(family, style)
        missing |= characters - set(pdf.current_font.cmap)
    if missing:
        fallbacks = []
        for path in paths:
            if not path.is_file():
                continue
            name = f"OfficeFallback{len(fallbacks)}"
            pdf.add_font(name, fname=path)
            pdf.set_font(name)
            fallbacks.append(name)
            missing -= set(pdf.current_font.cmap)
            if not missing:
                break
        if missing:
            raise ValueError("The available PDF fonts do not cover all document characters.")
        pdf.set_fallback_fonts(fallbacks, exact_match=False)


def document_pdf(kind: str, record: dict) -> Path:
    """Generate and atomically persist the PDF for one quote revision or invoice."""
    from fpdf import FPDF, FontFace, XPos, YPos

    snapshot = json.loads(record["document"])
    lines = json.loads(record["lines"])
    created = datetime.fromisoformat(record["created_at"])
    number = record["number"]
    revision = f" / revision {record['revision']}" if kind == "quote" else ""
    due_label = "Valid through" if kind == "quote" else "Payment due"
    due = record["valid_until"] if kind == "quote" else record["due_at"]
    currency = record["currency"]
    title = f"{kind.title()} {number}"
    pdf = FPDF(format="A4")
    pdf.set_margins(17, 16, 17)
    pdf.set_auto_page_break(True, margin=17)
    pdf.set_title(title)
    pdf.set_author(snapshot["business_name"])
    pdf.set_creation_date(created)
    pdf.add_page()
    family, paths = _fonts(pdf)
    payment = snapshot["payment_text"] if kind == "invoice" else ""
    _support_text(pdf, family, paths, "\n".join((
        snapshot["business_name"], snapshot["customer_name"], snapshot["site_address"], payment,
        *(line["description"] for line in lines),
    )))

    def text(value: str, size: int, *, bold: bool = False, color=INK, height: float = 5):
        pdf.set_font(family, style="B" if bold else "", size=size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, height, value, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_fill_color(*ACCENT)
    pdf.rect(pdf.l_margin, pdf.get_y(), pdf.epw, 1.5, style="F")
    pdf.ln(6)
    text(snapshot["business_name"], 11, bold=True, color=ACCENT)
    text(title, 25, bold=True, height=12)
    text(f"{snapshot['job_number']}{revision}   |   {due_label} {due}", 10, color=MUTED)
    pdf.ln(5)
    text("Prepared for", 9, bold=True, color=ACCENT)
    text(snapshot["customer_name"], 11, bold=True)
    if snapshot["site_address"]:
        text(snapshot["site_address"], 10, color=MUTED)
    pdf.ln(6)

    pdf.set_font(family, size=9)
    pdf.set_text_color(*INK)
    pdf.set_draw_color(211, 225, 222)
    pdf.set_fill_color(255, 255, 255)
    with pdf.table(
        col_widths=(80, 25, 34, 37), text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"),
        line_height=5, padding=2, borders_layout="HORIZONTAL_LINES",
        headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=ACCENT),
    ) as table:
        table.row(("Description", "Qty", f"Unit ({currency})", f"Amount ({currency})"))
        for line in lines:
            table.row((line["description"], line["qty"], major(line["unit_price_minor"], currency),
                       major(line["total_minor"], currency)))

    if pdf.will_page_break(28):
        pdf.add_page()
    pdf.ln(5)
    subtotal = [("Subtotal", major(record["subtotal_minor"], currency))]
    if Decimal(record["tax_rate"]) > 0:
        subtotal.append((f"Tax ({record['tax_rate']}%)", major(record["tax_minor"], currency)))
    pdf.set_font(family, size=10)
    for label, amount in subtotal:
        pdf.cell(pdf.epw - 42, 6, label, align="R")
        pdf.cell(42, 6, amount, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(family, style="B", size=13)
    pdf.set_fill_color(*PALE)
    pdf.cell(pdf.epw - 42, 9, "Total", fill=True, align="R")
    pdf.cell(42, 9, f"{currency} {major(record['total_minor'], currency)}", fill=True, align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if payment:
        pdf.ln(6)
        text("Payment details", 10, bold=True, color=ACCENT)
        text(payment, 10)
    filename = f"{number}-r{record['revision']}.pdf" if kind == "quote" else f"{number}.pdf"
    return save_bytes("documents", filename, bytes(pdf.output()))
