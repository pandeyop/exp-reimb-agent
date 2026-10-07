"""Generates the deterministic demo dataset in demo_receipts/ plus fixtures.json.

All merchants, people, GSTINs and numbers are fictional. fixtures.json holds the recorded
extraction for each file (keyed by SHA-256) that the offline mode replays; with an Anthropic key
the same files are read live by Claude instead.

Run:  backend/.venv/Scripts/python scripts/generate_demo_receipts.py
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo_receipts"
OUT.mkdir(exist_ok=True)
random.seed(7)

FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")]


def font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        for n in names:
            p = d / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


MONO = lambda s: font(["consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"], s)  # noqa: E731
MONOB = lambda s: font(["consolab.ttf", "courbd.ttf", "DejaVuSansMono-Bold.ttf"], s)  # noqa: E731
SANS = lambda s: font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], s)  # noqa: E731
SANSB = lambda s: font(["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"], s)  # noqa: E731


# ------------------------------------------------------------------ image helpers

def thermal_receipt(lines: list[tuple[str, str]], width=560, paper=(250, 248, 240)) -> Image.Image:
    """lines: (style, text) where style in {'c','cb','l','lb','r','sep','gap'}"""
    pad, lh = 28, 30
    h = pad * 2 + sum(14 if s == "gap" else lh for s, _ in lines)
    img = Image.new("RGB", (width, h), paper)
    d = ImageDraw.Draw(img)
    y = pad
    for style, text in lines:
        if style == "gap":
            y += 14
            continue
        f = MONOB(24) if style.endswith("b") else MONO(21)
        if style == "sep":
            d.text((pad, y), "-" * 40, font=MONO(21), fill=(60, 60, 60))
        elif style.startswith("c"):
            w = d.textlength(text, font=f)
            d.text(((width - w) / 2, y), text, font=f, fill=(25, 25, 25))
        elif style == "lr":
            left, right = text.split("|")
            d.text((pad, y), left, font=f, fill=(25, 25, 25))
            w = d.textlength(right, font=f)
            d.text((width - pad - w, y), right, font=f, fill=(25, 25, 25))
        else:
            d.text((pad, y), text, font=f, fill=(25, 25, 25))
        y += lh
    return img


def phone_photo(receipt: Image.Image, angle=3.5, bg=(92, 74, 60), shadow=True) -> Image.Image:
    W, H = receipt.width + 260, receipt.height + 240
    canvas = Image.new("RGB", (W, H), bg)
    noise = Image.effect_noise((W, H), 18).convert("RGB")
    canvas = Image.blend(canvas, noise, 0.12)
    rot = receipt.rotate(angle, expand=True, fillcolor=bg, resample=Image.BICUBIC)
    canvas.paste(rot, ((W - rot.width) // 2, (H - rot.height) // 2))
    if shadow:
        grad = Image.linear_gradient("L").resize((W, H)).rotate(35)
        dark = Image.new("RGB", (W, H), (0, 0, 0))
        canvas = Image.composite(dark, canvas, grad.point(lambda v: int(v * 0.28)))
    return canvas.filter(ImageFilter.GaussianBlur(0.6))


def save_jpg(img: Image.Image, name: str, quality=86) -> Path:
    p = OUT / name
    img.save(p, "JPEG", quality=quality)
    return p


# ------------------------------------------------------------------ PDF helpers

class Doc(FPDF):
    def heading(self, text, size=16):
        self.set_font("Helvetica", "B", size)
        self.cell(0, 9, text, new_x="LMARGIN", new_y="NEXT")

    def para(self, text, size=10, style=""):
        self.set_font("Helvetica", style, size)
        self.multi_cell(0, 5.5, text, new_x="LMARGIN", new_y="NEXT")

    def row(self, cells, widths, bold=False, fill=False):
        self.set_font("Helvetica", "B" if bold else "", 9.5)
        if fill:
            self.set_fill_color(235, 239, 245)
        for c, w in zip(cells, widths):
            align = "R" if c[:1].isdigit() or c.startswith("Rs") or c.startswith("INR") else "L"
            self.cell(w, 7, c, border=1, align=align, fill=fill)
        self.ln()


def save_pdf(pdf: FPDF, name: str) -> Path:
    p = OUT / name
    pdf.output(str(p))
    return p


# ------------------------------------------------------------------ documents

def f(v, c):
    return {"value": None if v is None else str(v), "confidence": c if v is not None else 0.0}


ALL_FIELDS = ["merchant", "invoiceNumber", "expenseDate", "currency", "subtotal", "tax", "serviceCharge", "tip", "total",
              "merchantAddress", "city", "gstin", "paymentMethod", "cardLast4", "bookingReference", "origin", "destination",
              "travelDate", "returnDate", "checkIn", "checkOut", "nights", "covers", "billingPeriod", "businessPurpose",
              "attendees", "description"]


def extraction(doc_type, readability, fields: dict, category, cat_conf, reason, line_items=(), notes=(), multiple_totals=False):
    full = {k: f(None, 0) for k in ALL_FIELDS}
    full.update(fields)
    return {
        "isExpenseDocument": True, "documentType": doc_type, "readability": readability, "fields": full,
        "lineItems": [{"description": d, "quantity": q, "unitPrice": u, "amount": a} for d, q, u, a in line_items],
        "categorySuggestion": {"category": category, "confidence": cat_conf, "reason": reason},
        "multipleTotalsFound": multiple_totals, "notes": list(notes),
    }


def doc01_air() -> tuple[Path, dict]:
    pdf = Doc()
    pdf.add_page()
    pdf.set_text_color(20, 40, 90)
    pdf.heading("AirVista Airlines", 20)
    pdf.set_text_color(0, 0, 0)
    pdf.para("AirVista Aviation Pvt Ltd | GSTIN: 07AAVCA4417K1Z3 | Aerocity, New Delhi 110037")
    pdf.ln(3)
    pdf.heading("TAX INVOICE - Air Ticket", 13)
    pdf.para("Invoice No: AV-INV-2609-118742        Invoice Date: 15-09-2026\nPNR: K7QX2M        Booked via: Demo India Corp Travel Desk")
    pdf.para("Passenger: Demo Employee (EMP001)        Corporate account: DEMO INDIA CORP")
    pdf.para("Trip purpose (from travel request TR-5521): Customer meetings - Northwind Retail contract renewal", style="B")
    pdf.ln(2)
    pdf.row(["Flight", "Sector", "Date", "Dep", "Arr", "Class"], [25, 55, 30, 20, 20, 30], bold=True, fill=True)
    pdf.row(["AV 512", "DEL (Delhi) - BLR (Bengaluru)", "21-09-2026", "07:10", "09:55", "Economy"], [25, 55, 30, 20, 20, 30])
    pdf.row(["AV 517", "BLR (Bengaluru) - DEL (Delhi)", "24-09-2026", "19:40", "22:25", "Economy"], [25, 55, 30, 20, 20, 30])
    pdf.ln(3)
    pdf.row(["Description", "Amount (INR)"], [140, 40], bold=True, fill=True)
    for d, a in [("Base fare (2 sectors)", "9,780.00"), ("Airport & user development fees", "776.00"), ("Convenience fee", "200.00"),
                 ("IGST @ 5% on fare", "484.00")]:
        pdf.row([d, a], [140, 40])
    pdf.row(["Total amount payable (INR)", "11,240.00"], [140, 40], bold=True)
    pdf.ln(3)
    pdf.para("Paid by: Corporate card ending 4417. This is a computer-generated invoice and does not require a signature.", 8)
    p = save_pdf(pdf, "01_airvista_flight_invoice.pdf")
    ex = extraction("air_ticket", "good", {
        "merchant": f("AirVista Airlines", 0.98), "invoiceNumber": f("AV-INV-2609-118742", 0.97),
        "expenseDate": f("2026-09-21", 0.93), "currency": f("INR", 0.99), "subtotal": f("10756.00", 0.9), "tax": f("484.00", 0.95),
        "total": f("11240.00", 0.99), "merchantAddress": f("Aerocity, New Delhi 110037", 0.92), "gstin": f("07AAVCA4417K1Z3", 0.96),
        "paymentMethod": f("Corporate card", 0.93), "cardLast4": f("4417", 0.95), "bookingReference": f("K7QX2M", 0.98),
        "origin": f("Delhi (DEL)", 0.97), "destination": f("Bengaluru (BLR)", 0.97), "travelDate": f("2026-09-21", 0.97),
        "returnDate": f("2026-09-24", 0.96), "city": f("Bengaluru", 0.9),
        "businessPurpose": f("Customer meetings - Northwind Retail contract renewal", 0.95),
        "description": f("Return flight DEL-BLR-DEL, AV 512 / AV 517", 0.9),
    }, "AIR_TRAVEL", 0.98, "Airline tax invoice with a PNR, flight numbers and DEL-BLR sectors.",
        [("Base fare (2 sectors)", 1, 9780, 9780), ("Airport & user development fees", 1, 776, 776), ("Convenience fee", 1, 200, 200), ("IGST @ 5%", 1, 484, 484)])
    return p, ex


def doc02_hotel() -> tuple[Path, dict]:
    pdf = Doc()
    pdf.add_page()
    pdf.set_text_color(90, 40, 60)
    pdf.heading("The Orchid Grand", 20)
    pdf.set_text_color(0, 0, 0)
    pdf.para("Orchid Grand Hospitality LLP | 14 ITPL Main Road, Whitefield, Bengaluru 560066 | GSTIN 29AAKFO8812M1Z5")
    pdf.ln(3)
    pdf.heading("GUEST FOLIO / TAX INVOICE (Page 1 of 2)", 12)
    pdf.para("Invoice No: OGW/26-27/04418        Folio: 77231        Room: 614 (Deluxe King)\nGuest: Demo Employee        Company: Demo India Corp\nArrival: 21-09-2026 14:05        Departure: 24-09-2026 11:20        Nights: 3")
    pdf.ln(2)
    w = [28, 92, 30, 30]
    pdf.row(["Date", "Description", "Charges", "Credits"], w, bold=True, fill=True)
    for d in ("21-09-2026", "22-09-2026", "23-09-2026"):
        pdf.row([d, "Room tariff - Deluxe King (CP)", "7,800.00", ""], w)
        pdf.row([d, "CGST 2.5% + SGST 2.5% on room", "390.00", ""], w)
    pdf.row(["24-09-2026", "Settlement - Visa ****9031", "", "24,570.00"], w)
    pdf.ln(2)
    pdf.para("Continued on next page ...", 9, "I")
    pdf.add_page()
    pdf.heading("GUEST FOLIO / TAX INVOICE (Page 2 of 2)", 12)
    pdf.para("Invoice No: OGW/26-27/04418        Guest: Demo Employee")
    pdf.ln(2)
    pdf.row(["Summary", "", "Amount (INR)", ""], w, bold=True, fill=True)
    pdf.row(["", "Room charges (3 nights x 7,800.00)", "23,400.00", ""], w)
    pdf.row(["", "CGST @ 2.5%", "585.00", ""], w)
    pdf.row(["", "SGST @ 2.5%", "585.00", ""], w)
    pdf.row(["", "Grand total", "24,570.00", ""], w, bold=True)
    pdf.row(["", "Balance due", "0.00", ""], w)
    pdf.ln(4)
    pdf.para("Thank you for staying with us. Check-out time 12:00. SAC 996311.", 9)
    p = save_pdf(pdf, "02_orchid_grand_hotel_folio.pdf")
    ex = extraction("hotel_invoice", "good", {
        "merchant": f("The Orchid Grand", 0.97), "invoiceNumber": f("OGW/26-27/04418", 0.96), "expenseDate": f("2026-09-24", 0.92),
        "currency": f("INR", 0.99), "subtotal": f("23400.00", 0.96), "tax": f("1170.00", 0.95), "total": f("24570.00", 0.98),
        "merchantAddress": f("14 ITPL Main Road, Whitefield, Bengaluru 560066", 0.95), "city": f("Bengaluru", 0.96),
        "gstin": f("29AAKFO8812M1Z5", 0.95), "paymentMethod": f("Visa card", 0.9), "cardLast4": f("9031", 0.93),
        "checkIn": f("2026-09-21", 0.97), "checkOut": f("2026-09-24", 0.97), "nights": f("3", 0.98),
        "description": f("3 nights, Deluxe King room 614", 0.9),
    }, "HOTEL", 0.98, "Issued by a hotel and contains room-night charges for 3 nights.",
        [("Room tariff - Deluxe King", 3, 7800, 23400), ("CGST 2.5%", 1, 585, 585), ("SGST 2.5%", 1, 585, 585)])
    return p, ex


ZIPCAB_LINES = [
    ("cb", "ZIPCAB"), ("c", "Ride Receipt / Tax Invoice"), ("c", "ZipCab Mobility Pvt Ltd"), ("c", "GSTIN 29AAJCZ5521Q1Z8"), ("sep", ""),
    ("l", "Invoice : ZC-4471-2109"), ("l", "Date    : 21 Sep 2026  10:42"), ("l", "Driver  : Manjunath K"), ("l", "Vehicle : KA-03-AK-7781 (Sedan)"), ("sep", ""),
    ("l", "Pickup : Kempegowda Intl Airport T1"), ("l", "Drop   : The Orchid Grand, Whitefield"), ("l", "Distance 41.2 km   Time 63 min"), ("sep", ""),
    ("lr", "Ride fare|Rs. 712.00"), ("lr", "Airport toll & parking|Rs. 37.00"), ("lr", "GST 5%|Rs. 37.00"), ("sep", ""),
    ("lr", "TOTAL|Rs. 786.00"), ("l", "Paid: Corporate card **** 4417"), ("gap", ""), ("c", "Thank you for riding with ZipCab"),
]
ZIPCAB_FIELDS = {
    "merchant": f("ZipCab", 0.96), "invoiceNumber": f("ZC-4471-2109", 0.93), "expenseDate": f("2026-09-21", 0.94),
    "currency": f("INR", 0.97), "tax": f("37.00", 0.9), "total": f("786.00", 0.96), "gstin": f("29AAJCZ5521Q1Z8", 0.88),
    "paymentMethod": f("Corporate card", 0.9), "cardLast4": f("4417", 0.92), "origin": f("Kempegowda Intl Airport T1", 0.93),
    "destination": f("The Orchid Grand, Whitefield", 0.93), "city": f("Bengaluru", 0.9), "description": f("Airport to hotel", 0.85),
}


def doc03_zipcab_rotated() -> tuple[Path, dict]:
    img = phone_photo(thermal_receipt(ZIPCAB_LINES), angle=1.5, bg=(70, 82, 96), shadow=False).rotate(90, expand=True)
    p = save_jpg(img, "03_zipcab_airport_ride_rotated.jpg")
    ex = extraction("ride_receipt", "good", ZIPCAB_FIELDS, "TAXI", 0.95,
                    "Ride-hailing receipt with pickup at the airport and drop at a hotel.",
                    [("Ride fare", 1, 712, 712), ("Airport toll & parking", 1, 37, 37), ("GST 5%", 1, 37, 37)],
                    notes=["Image was rotated 90 degrees; read after re-orienting."])
    return p, ex


def doc04_coffee_blurry() -> tuple[Path, dict]:
    lines = [("cb", "BEAN STREET COFFEE"), ("c", "Brigade Road, Bengaluru"), ("c", "GSTIN 29AAOFB3361D1Z0"), ("sep", ""),
             ("l", "Bill: BSC/2309/0412   Tbl: TA"), ("l", "Date: 23/09/2026   08:47"), ("sep", ""),
             ("lr", "1 Cappuccino (L)|Rs. 220.00"), ("lr", "1 Almond croissant|Rs. 180.00"), ("lr", "CGST 2.5%|Rs. 10.00"), ("lr", "SGST 2.5%|Rs. 10.00"), ("sep", ""),
             ("lr", "TOTAL|Rs. 420.00"), ("l", "UPI"), ("gap", ""), ("c", "Have a great day!")]
    img = phone_photo(thermal_receipt(lines), angle=-2.0, bg=(150, 140, 125)).filter(ImageFilter.GaussianBlur(1.8))
    p = save_jpg(img, "04_bean_street_coffee_blurry.jpg", quality=72)
    ex = extraction("cafe_receipt", "fair", {
        "merchant": f("Bean Street Coffee", 0.86), "invoiceNumber": f("BSC/2309/0412", 0.72), "expenseDate": f("2026-09-23", 0.78),
        "currency": f("INR", 0.95), "subtotal": f("400.00", 0.8), "tax": f("20.00", 0.75), "total": f("420.00", 0.92),
        "merchantAddress": f("Brigade Road, Bengaluru", 0.8), "city": f("Bengaluru", 0.85), "paymentMethod": f("UPI", 0.8),
        "covers": f("1", 0.6), "description": f("Coffee and croissant", 0.8),
    }, "MEALS", 0.93, "Café receipt listing a coffee and a pastry.",
        [("Cappuccino (L)", 1, 220, 220), ("Almond croissant", 1, 180, 180)], notes=["Image is blurred; date digits partly soft."])
    return p, ex


def doc05_dinner_photo() -> tuple[Path, dict]:
    lines = [("cb", "HOPS & BARRELS"), ("c", "Brewpub & Kitchen"), ("c", "EPIP Zone, Whitefield, Bengaluru"), ("c", "GSTIN 29AAHFH2290B1Z7"), ("sep", ""),
             ("l", "Bill No: HB-22091-0387"), ("l", "Date: 22-Sep-2026  21:14"), ("l", "Table: 12    Covers: 4    Server: Arjun"), ("sep", ""),
             ("lr", "2 Paneer tikka platter|Rs. 640.00"), ("lr", "1 Lamb biryani|Rs. 420.00"), ("lr", "2 Veg thai curry bowl|Rs. 560.00"),
             ("lr", "4 Fresh lime soda|Rs. 140.00"), ("sep", ""), ("lr", "Subtotal|Rs. 1,760.00"), ("lr", "CGST 2.5%|Rs. 44.00"), ("lr", "SGST 2.5%|Rs. 44.00"),
             ("lr", "Round off|Rs. 2.00"), ("sep", ""), ("lr", "GRAND TOTAL|Rs. 1,850.00"), ("l", "Paid: Card **** 4417"), ("gap", ""), ("c", "Cheers! Visit again")]
    img = phone_photo(thermal_receipt(lines), angle=4.0, bg=(84, 58, 40))
    w, h = img.size
    img = img.transform((w, h), Image.QUAD, (30, 0, 0, h - 30, w, h, w - 10, 25), resample=Image.BICUBIC)
    p = save_jpg(img, "05_hops_and_barrels_dinner_photo.jpg")
    ex = extraction("restaurant_bill", "good", {
        "merchant": f("Hops & Barrels", 0.95), "invoiceNumber": f("HB-22091-0387", 0.9), "expenseDate": f("2026-09-22", 0.93),
        "currency": f("INR", 0.97), "subtotal": f("1760.00", 0.92), "tax": f("88.00", 0.9), "total": f("1850.00", 0.95),
        "merchantAddress": f("EPIP Zone, Whitefield, Bengaluru", 0.9), "city": f("Bengaluru", 0.92), "gstin": f("29AAHFH2290B1Z7", 0.86),
        "paymentMethod": f("Card", 0.88), "cardLast4": f("4417", 0.9), "covers": f("4", 0.9), "description": f("Dinner for 4", 0.85),
    }, "MEALS", 0.93, "Restaurant bill listing food and drinks for 4 covers.",
        [("Paneer tikka platter", 2, 320, 640), ("Lamb biryani", 1, 420, 420), ("Veg thai curry bowl", 2, 280, 560), ("Fresh lime soda", 4, 35, 140)])
    return p, ex


def ride_pdf(name, inv, date_txt, time_txt, pickup, drop, km, fare, gst, total) -> Path:
    pdf = Doc(format=(105, 190))
    pdf.set_margins(8, 8, 8)
    pdf.add_page()
    pdf.set_text_color(10, 120, 90)
    pdf.heading("RideNow", 18)
    pdf.set_text_color(0, 0, 0)
    pdf.para("Your trip receipt\nRideNow Technologies Pvt Ltd | GSTIN 29AAGCR7733L1Z2", 8.5)
    pdf.ln(2)
    pdf.para(f"Invoice No: {inv}\nTrip date: {date_txt}, {time_txt}\nPickup: {pickup}\nDrop: {drop}\nDistance: {km} km   Category: Sedan", 9)
    pdf.ln(2)
    pdf.row(["Trip fare", f"INR {fare:,.2f}"], [55, 34])
    pdf.row(["GST @ 5%", f"INR {gst:,.2f}"], [55, 34])
    pdf.row(["Total paid", f"INR {total:,.2f}"], [55, 34], bold=True)
    pdf.ln(2)
    pdf.para("Payment: Corporate card **** 4417", 8.5)
    return save_pdf(pdf, name)


def doc06_ridenow() -> tuple[Path, dict]:
    p = ride_pdf("06_ridenow_trip_receipt.pdf", "RN-88214-0922", "22 Sep 2026", "09:12", "The Orchid Grand, ITPL Main Road, Whitefield",
                 "Northwind Retail HQ, Tech Park One, Whitefield", 6.8, 611.43, 30.57, 642.00)
    ex = extraction("ride_receipt", "good", {
        "merchant": f("RideNow", 0.97), "invoiceNumber": f("RN-88214-0922", 0.96), "expenseDate": f("2026-09-22", 0.97),
        "currency": f("INR", 0.99), "subtotal": f("611.43", 0.95), "tax": f("30.57", 0.95), "total": f("642.00", 0.99),
        "gstin": f("29AAGCR7733L1Z2", 0.95), "paymentMethod": f("Corporate card", 0.95), "cardLast4": f("4417", 0.97),
        "origin": f("The Orchid Grand, Whitefield", 0.96), "destination": f("Northwind Retail HQ, Tech Park One, Whitefield", 0.96),
        "city": f("Bengaluru", 0.9), "description": f("Hotel to customer office", 0.88),
    }, "TAXI", 0.97, "Ride-hailing receipt with pickup and drop locations.")
    return p, ex


def doc07_zipcab_copy() -> tuple[Path, dict]:
    pdf = Doc(format=(105, 200))
    pdf.set_margins(8, 8, 8)
    pdf.add_page()
    pdf.heading("ZipCab", 18)
    pdf.para("Ride receipt (emailed copy)\nZipCab Mobility Pvt Ltd | GSTIN 29AAJCZ5521Q1Z8", 8.5)
    pdf.ln(2)
    pdf.para("Invoice: ZC-4471-2109\nDate: 21 Sep 2026, 10:42\nPickup: Kempegowda Intl Airport T1\nDrop: The Orchid Grand, Whitefield\nDistance: 41.2 km", 9)
    pdf.ln(2)
    pdf.row(["Ride fare", "INR 712.00"], [55, 34])
    pdf.row(["Airport toll & parking", "INR 37.00"], [55, 34])
    pdf.row(["GST 5%", "INR 37.00"], [55, 34])
    pdf.row(["Total", "INR 786.00"], [55, 34], bold=True)
    pdf.para("Paid with corporate card **** 4417", 8.5)
    p = save_pdf(pdf, "07_zipcab_receipt_email_copy.pdf")
    fields = dict(ZIPCAB_FIELDS)
    fields.update({"merchant": f("ZipCab", 0.98), "invoiceNumber": f("ZC-4471-2109", 0.98), "total": f("786.00", 0.99),
                   "expenseDate": f("2026-09-21", 0.98)})
    ex = extraction("ride_receipt", "good", fields, "TAXI", 0.97, "Ride-hailing receipt with pickup and drop locations.",
                    [("Ride fare", 1, 712, 712), ("Airport toll & parking", 1, 37, 37), ("GST 5%", 1, 37, 37)])
    return p, ex


def doc08_parking_smudged() -> tuple[Path, dict]:
    lines = [("cb", "TECH PARK ONE"), ("c", "Visitor Parking - Basement B2"), ("c", "Whitefield, Bengaluru"), ("sep", ""),
             ("l", "Ticket : P-B2-30917"), ("l", "Vehicle: KA-05-MN-2290 (Car)"), ("l", "Date   : ##/##/2026"), ("l", "In 09:31   Out 18:05"), ("sep", ""),
             ("lr", "Parking charges|Rs. 150.00"), ("sep", ""), ("lr", "AMOUNT PAID|Rs. 150.00"), ("l", "Cash")]
    rec = thermal_receipt(lines, paper=(238, 236, 222))
    d = ImageDraw.Draw(rec)
    d.ellipse((118, 200, 262, 238), fill=(120, 110, 140))  # ink smudge over the date
    rec = rec.filter(ImageFilter.GaussianBlur(0.4))
    img = phone_photo(rec, angle=-3.0, bg=(110, 110, 105))
    p = save_jpg(img, "08_techpark_parking_slip.jpg")
    ex = extraction("parking_slip", "fair", {
        "merchant": f("Tech Park One Visitor Parking", 0.93), "invoiceNumber": f("P-B2-30917", 0.88), "expenseDate": f(None, 0),
        "currency": f("INR", 0.95), "total": f("150.00", 0.94), "merchantAddress": f("Whitefield, Bengaluru", 0.9),
        "city": f("Bengaluru", 0.88), "paymentMethod": f("Cash", 0.9), "description": f("Visitor parking, 09:31-18:05", 0.85),
    }, "PARKING", 0.96, "Parking ticket with vehicle entry and exit times.",
        notes=["The date on the slip is covered by an ink smudge and cannot be read."])
    return p, ex


def doc09_ridenow_screenshot() -> tuple[Path, dict]:
    W, H = 720, 1280
    img = Image.new("RGB", (W, H), (246, 248, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 150), fill=(10, 120, 90))
    d.text((40, 50), "RideNow", font=SANSB(52), fill="white")
    y = 200
    d.text((40, y), "Thanks for riding, Demo", font=SANSB(36), fill=(20, 20, 20)); y += 70
    d.text((40, y), "Thu, 24 Sep 2026 · 16:05", font=SANS(28), fill=(90, 90, 90)); y += 60
    d.rounded_rectangle((30, y, W - 30, y + 230), 18, fill="white", outline=(220, 220, 220)); y += 30
    d.text((60, y), "● The Orchid Grand, Whitefield", font=SANS(28), fill=(20, 20, 20)); y += 60
    d.text((60, y), "■ Kempegowda Intl Airport T1", font=SANS(28), fill=(20, 20, 20)); y += 60
    d.text((60, y), "39.6 km · 71 min · Sedan", font=SANS(24), fill=(110, 110, 110)); y += 110
    for label_, amt in [("Trip fare", "₹866.67"), ("GST @ 5%", "₹43.33")]:
        d.text((40, y), label_, font=SANS(30), fill=(40, 40, 40)); d.text((W - 220, y), amt, font=SANS(30), fill=(40, 40, 40)); y += 55
    d.line((40, y, W - 40, y), fill=(200, 200, 200), width=2); y += 25
    d.text((40, y), "Total", font=SANSB(38), fill=(20, 20, 20)); d.text((W - 240, y), "₹910.00", font=SANSB(38), fill=(20, 20, 20)); y += 80
    d.text((40, y), "Invoice RN-90551-0924 · Corporate card •••• 4417", font=SANS(24), fill=(110, 110, 110))
    p = OUT / "09_ridenow_airport_drop_screenshot.png"
    img.save(p, "PNG")
    ex = extraction("ride_receipt", "good", {
        "merchant": f("RideNow", 0.97), "invoiceNumber": f("RN-90551-0924", 0.94), "expenseDate": f("2026-09-24", 0.96),
        "currency": f("INR", 0.98), "subtotal": f("866.67", 0.93), "tax": f("43.33", 0.93), "total": f("910.00", 0.98),
        "paymentMethod": f("Corporate card", 0.93), "cardLast4": f("4417", 0.95), "origin": f("The Orchid Grand, Whitefield", 0.95),
        "destination": f("Kempegowda Intl Airport T1", 0.95), "city": f("Bengaluru", 0.9), "description": f("Hotel to airport", 0.88),
    }, "TAXI", 0.97, "Ride-hailing app receipt with pickup and drop locations.")
    return p, ex


def doc10_udupi_lunch() -> tuple[Path, dict]:
    lines = [("cb", "UDUPI UPAHAR"), ("c", "Pure Veg Restaurant"), ("c", "ITPL Road, Whitefield, Bengaluru"), ("sep", ""),
             ("l", "Bill: UU-7730   23/09/2026 13:22"), ("l", "Covers: 1"), ("sep", ""),
             ("lr", "1 South Indian thali|Rs. 290.00"), ("lr", "1 Filter coffee|Rs. 72.00"), ("lr", "GST 5%|Rs. 18.00"), ("sep", ""),
             ("lr", "TOTAL|Rs. 380.00"), ("l", "Paid UPI")]
    img = phone_photo(thermal_receipt(lines), angle=-1.0, bg=(120, 96, 70))
    p = save_jpg(img, "10_udupi_upahar_lunch.jpg")
    ex = extraction("restaurant_bill", "good", {
        "merchant": f("Udupi Upahar", 0.95), "invoiceNumber": f("UU-7730", 0.9), "expenseDate": f("2026-09-23", 0.94),
        "currency": f("INR", 0.97), "subtotal": f("362.00", 0.9), "tax": f("18.00", 0.9), "total": f("380.00", 0.96),
        "merchantAddress": f("ITPL Road, Whitefield, Bengaluru", 0.9), "city": f("Bengaluru", 0.92), "paymentMethod": f("UPI", 0.9),
        "covers": f("1", 0.92), "description": f("Lunch", 0.85),
    }, "MEALS", 0.95, "Restaurant bill for a single thali lunch.", [("South Indian thali", 1, 290, 290), ("Filter coffee", 1, 72, 72)])
    return p, ex


def doc11_mobile_bill() -> tuple[Path, dict]:
    pdf = Doc()
    pdf.add_page()
    pdf.set_text_color(200, 30, 60)
    pdf.heading("Konnect Mobile", 20)
    pdf.set_text_color(0, 0, 0)
    pdf.para("Konnect Telecom Ltd | GSTIN 29AACCK1180P1Z9 | Koramangala, Bengaluru 560034")
    pdf.ln(2)
    pdf.heading("Postpaid Bill / Tax Invoice", 13)
    pdf.para("Invoice No: KM-BLR-2609-60417        Bill date: 05-09-2026\nAccount: 9000XXXX21 (Demo Employee)        Bill period: 01-08-2026 to 31-08-2026\nPlan: Business Unlimited 999")
    pdf.ln(2)
    w = [140, 40]
    pdf.row(["Charges", "Amount (INR)"], w, bold=True, fill=True)
    pdf.row(["Monthly plan rental - Business Unlimited 999", "999.00"], w)
    pdf.row(["CGST @ 9%", "89.91"], w)
    pdf.row(["SGST @ 9%", "89.91"], w)
    pdf.row(["Rounding", "0.18"], w)
    pdf.row(["Total amount payable", "1,179.00"], w, bold=True)
    pdf.ln(2)
    pdf.para("Data usage 38.2 GB | Calls unlimited | Due date 20-09-2026. Paid via auto-debit.", 9)
    p = save_pdf(pdf, "11_konnect_mobile_bill_aug.pdf")
    ex = extraction("telecom_bill", "good", {
        "merchant": f("Konnect Mobile", 0.97), "invoiceNumber": f("KM-BLR-2609-60417", 0.96), "expenseDate": f("2026-09-05", 0.95),
        "currency": f("INR", 0.99), "subtotal": f("999.00", 0.96), "tax": f("179.82", 0.94), "total": f("1179.00", 0.98),
        "merchantAddress": f("Koramangala, Bengaluru 560034", 0.9), "gstin": f("29AACCK1180P1Z9", 0.95),
        "paymentMethod": f("Auto-debit", 0.9), "billingPeriod": f("01 Aug 2026 - 31 Aug 2026", 0.95),
        "description": f("Postpaid plan Business Unlimited 999 (August)", 0.9),
    }, "MOBILE_INTERNET", 0.97, "Telecom postpaid bill with a billing period and plan rental.")
    return p, ex


def doc12_stationers_scan() -> tuple[Path, dict]:
    lines = [("cb", "CITY STATIONERS & IT"), ("c", "Commercial Street, Bengaluru"), ("c", "GSTIN 29AAEPC6634H1Z1"), ("c", "TAX INVOICE"), ("sep", ""),
             ("l", "Inv No: CS/1452/26   Dt: 22.09.2026"), ("sep", ""),
             ("lr", "1 Wireless presenter/clicker|Rs. 1,099.00"), ("lr", "1 USB-C to HDMI adapter|Rs. 130.00"),
             ("lr", "CGST 9%|Rs. 110.61"), ("lr", "SGST 9%|Rs. 110.61"), ("lr", "Round off|Rs. -0.22"), ("sep", ""),
             ("lr", "NET PAYABLE|Rs. 1,450.00"), ("l", "Card **** 4417")]
    rec = thermal_receipt(lines, paper=(255, 255, 255)).convert("L")
    scan = Image.new("L", (rec.width + 120, rec.height + 120), 235)
    scan.paste(rec, (60, 60))
    noise = Image.effect_noise(scan.size, 10)
    scan = Image.blend(scan, noise, 0.06).rotate(-0.8, fillcolor=235)
    p = OUT / "12_city_stationers_scan.png"
    scan.save(p, "PNG")
    ex = extraction("retail_invoice", "good", {
        "merchant": f("City Stationers & IT", 0.94), "invoiceNumber": f("CS/1452/26", 0.92), "expenseDate": f("2026-09-22", 0.93),
        "currency": f("INR", 0.97), "subtotal": f("1229.00", 0.9), "tax": f("221.22", 0.9), "total": f("1450.00", 0.96),
        "merchantAddress": f("Commercial Street, Bengaluru", 0.9), "city": f("Bengaluru", 0.92), "gstin": f("29AAEPC6634H1Z1", 0.9),
        "paymentMethod": f("Card", 0.9), "cardLast4": f("4417", 0.92), "description": f("Wireless presenter and USB-C to HDMI adapter", 0.9),
    }, "OFFICE_SUPPLIES", 0.94, "Retail tax invoice for a presentation clicker and display adapter.",
        [("Wireless presenter/clicker", 1, 1099, 1099), ("USB-C to HDMI adapter", 1, 130, 130)])
    return p, ex


def main() -> None:
    for old in OUT.glob("*"):
        if old.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg"):
            old.unlink()
    builders = [doc01_air, doc02_hotel, doc03_zipcab_rotated, doc04_coffee_blurry, doc05_dinner_photo, doc06_ridenow,
                doc07_zipcab_copy, doc08_parking_smudged, doc09_ridenow_screenshot, doc10_udupi_lunch, doc11_mobile_bill,
                doc12_stationers_scan]
    fixtures = {}
    for b in builders:
        path, ex = b()
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        fixtures[sha] = {"filename": path.name, "extraction": ex}
        print(f"{path.name:45s} {path.stat().st_size // 1024:5d} KB  {sha[:12]}")
    (OUT / "fixtures.json").write_text(json.dumps({
        "_comment": "Recorded extractions replayed in offline mode, keyed by file SHA-256. Regenerate with scripts/generate_demo_receipts.py.",
        "documents": fixtures}, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
