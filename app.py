
import os
import re
import json
import base64
import urllib.request
import urllib.error
import secrets
from io import BytesIO
from datetime import datetime
from collections import defaultdict, deque

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether
)

app = Flask(__name__)

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "").strip()
if FRONTEND_ORIGIN:
    CORS(app, resources={r"/api/*": {"origins": [FRONTEND_ORIGIN]}})
else:
    # Nur für die erste Einrichtung. Später FRONTEND_ORIGIN setzen.
    CORS(app, resources={r"/api/*": {"origins": "*"}})

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Feldmarker Karnevals-Komitee e.V.").strip()
BREVO_FROM_EMAIL = os.getenv("BREVO_FROM_EMAIL", "sebastian.petter@gmx.net").strip()
REPLY_TO = os.getenv("REPLY_TO", "protokoll.fkk@gmx.de").strip()
INTERNAL_RECIPIENT = os.getenv("INTERNAL_RECIPIENT", "protokoll.fkk@gmx.de").strip()

WELCOME_TEMPLATE = os.getenv("WELCOME_TEMPLATE", "welcome_email.html")
LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "fkk-logo.jpeg")

# Kleine In-Memory-Sperre gegen automatisierte Mehrfachsendungen.
RATE_LIMIT = defaultdict(deque)
RATE_MAX = 5
RATE_WINDOW_SECONDS = 3600

# Separates Rate-Limit für das Kontaktformular.
CONTACT_RATE_LIMIT = defaultdict(deque)
CONTACT_RATE_MAX = 10
CONTACT_RATE_WINDOW_SECONDS = 3600


def check_contact_rate_limit(ip):
    now = datetime.now().timestamp()
    q = CONTACT_RATE_LIMIT[ip]
    while q and now - q[0] > CONTACT_RATE_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= CONTACT_RATE_MAX:
        return False
    q.append(now)
    return True


def clean(value, max_len=500):
    if value is None:
        return ""
    value = str(value).strip()
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return value[:max_len]


def valid_email(value):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value or ""))


def normalize_iban(value):
    return re.sub(r"\s+", "", (value or "")).upper()


def valid_iban(value):
    iban = normalize_iban(value)
    if not (15 <= len(iban) <= 34):
        return False
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    remainder = 0
    for digit in converted:
        remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


def format_iban(value):
    iban = normalize_iban(value)
    return " ".join(iban[i:i+4] for i in range(0, len(iban), 4))


def format_date(value):
    if not value:
        return "-"
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def check_rate_limit(ip):
    now = datetime.now().timestamp()
    q = RATE_LIMIT[ip]
    while q and now - q[0] > RATE_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_MAX:
        return False
    q.append(now)
    return True


def make_application_pdf(data, application_no):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=16*mm, leftMargin=16*mm,
        topMargin=14*mm, bottomMargin=14*mm,
        title=f"FKK Aufnahmeantrag {application_no}"
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TitleFKK", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=22, leading=26, textColor=colors.HexColor("#0f6a34"),
        alignment=TA_CENTER, spaceAfter=8
    )
    sub = ParagraphStyle(
        "Sub", parent=styles["Normal"], fontSize=10, leading=14,
        textColor=colors.HexColor("#555555"), alignment=TA_CENTER, spaceAfter=12
    )
    h2 = ParagraphStyle(
        "H2FKK", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=13, leading=16, textColor=colors.HexColor("#b51f27"),
        spaceBefore=8, spaceAfter=6
    )
    body = ParagraphStyle(
        "BodyFKK", parent=styles["BodyText"], fontSize=9.6, leading=13.2,
        textColor=colors.HexColor("#222222")
    )
    small = ParagraphStyle(
        "SmallFKK", parent=body, fontSize=8.4, leading=11,
        textColor=colors.HexColor("#555555")
    )

    story = []
    if os.path.exists(LOGO_PATH):
        img = Image(LOGO_PATH, width=25*mm, height=25*mm)
        img.hAlign = "CENTER"
        story += [img, Spacer(1, 3*mm)]

    story.append(Paragraph("Aufnahmeantrag", title))
    story.append(Paragraph("Feldmarker Karnevals-Komitee e.V. · Wesel", sub))
    story.append(Paragraph(
        "Ich / Wir möchte(n) in das Feldmarker Karnevals-Komitee e.V. aufgenommen werden. "
        "Aufgabe und Ziel des FKK ist es, das Winterbrauchtum Karneval zu pflegen. "
        "Dazu ist uns jeder willkommen, der den nötigen Humor mitbringt. "
        "Ich / wir bin (sind) auch bereit, uns einer humorvollen Aufnahmezeremonie zu unterziehen.",
        body
    ))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph("Angaben zur Mitgliedschaft", h2))

    rows = [
        ["Mitgliedschaft", data["art"]],
        ["Vorname, Nachname", f'{data["vorname"]} {data["nachname"]}'],
        ["Adresse", data["adresse"]],
        ["Telefon", data["telefon"]],
        ["E-Mail", data["email"]],
        ["Geburtsdatum", format_date(data["geburt"])],
        ["Ehepartner / Geburtsdatum", format_date(data.get("ehe", ""))],
        ["IBAN", format_iban(data["iban"])],
        ["Antragsdatum", datetime.now().strftime("%d.%m.%Y")],
    ]

    table = Table(rows, colWidths=[54*mm, 108*mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#eef7f1")),
        ("TEXTCOLOR", (0,0), (0,-1), colors.HexColor("#0f6a34")),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTNAME", (1,0), (1,-1), "Helvetica"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("GRID", (0,0), (-1,-1), 0.35, colors.HexColor("#d9dedb")),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    story.append(table)

    story.append(Paragraph("Beiträge und Hinweise", h2))
    story.append(Paragraph(
        "Aktive Einzelmitglieder: 66,- EUR jährlich · Ehepaare: 99,- EUR jährlich · "
        "Jugendliche bis zum 16. Lebensjahr: 22,- EUR jährlich · Jugendliche bis zum "
        "18. Lebensjahr: 33,- EUR jährlich · Passive Mitglieder: 15,- EUR jährlich.",
        body
    ))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        "In diesem Beitrag ist der Eintritt zu unseren Karnevalsveranstaltungen enthalten "
        "(Übertrag auf andere Personen ausgeschlossen). Der Beitrag wird halbjährlich zum "
        "1. Mai und zum 1. November abgebucht.",
        body
    ))

    story.append(Paragraph("SEPA-Zustimmung", h2))
    story.append(Paragraph(
        "Ich / wir ermächtige(n) das Feldmarker Karnevals-Komitee e.V., die fälligen "
        "Mitgliedsbeiträge entsprechend dem erteilten SEPA-Lastschriftmandat von dem "
        "oben angegebenen Konto einzuziehen.",
        body
    ))

    story.append(Paragraph("Bestätigung des Online-Antrags", h2))
    story.append(Paragraph(
        "Dieser Antrag wurde über das Online-Anmeldeformular des Feldmarker Karnevals-Komitees e.V. "
        "übermittelt. Der Antragsteller hat die Richtigkeit der Angaben, die SEPA-Zustimmung, "
        "den Hinweis zur Aufnahmezeremonie und die Kenntnisnahme der Datenschutzhinweise bestätigt. "
        "Über die Aufnahme entscheidet der Vorstand.",
        body
    ))
    story.append(Spacer(1, 4*mm))

    meta = [
        ["Antragsnummer", application_no],
        ["Erstellt am", datetime.now().strftime("%d.%m.%Y %H:%M Uhr")],
    ]
    m = Table(meta, colWidths=[45*mm, 117*mm])
    m.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#fff8e1")),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 8.5),
        ("BOX", (0,0), (-1,-1), .4, colors.HexColor("#e5c958")),
        ("INNERGRID", (0,0), (-1,-1), .3, colors.HexColor("#eadfba")),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(m)
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("Stand der Beitragsangaben: 24.02.2026", small))

    doc.build(story)
    return buffer.getvalue()


def load_welcome_html():
    path = os.path.join(os.path.dirname(__file__), WELCOME_TEMPLATE)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Die Willkommensmail '{WELCOME_TEMPLATE}' fehlt im Backend-Ordner."
        )
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def brevo_send(*, to, subject, html=None, text=None, reply_to=None, attachments=None, recipient_name=None):
    """Versendet eine E-Mail über die Brevo-HTTPS-API (kein SMTP erforderlich)."""
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY ist auf dem Server noch nicht gesetzt.")

    payload = {
        "sender": {"name": MAIL_FROM_NAME, "email": BREVO_FROM_EMAIL},
        "to": [{"email": to, **({"name": recipient_name} if recipient_name else {})}],
        "subject": subject,
    }
    # Brevo erlaubt pro Request HTML oder Text. Für die Willkommensmail verwenden wir HTML,
    # für die interne Antrag-Mail Klartext.
    if html:
        payload["htmlContent"] = html
    elif text:
        payload["textContent"] = text
    else:
        payload["textContent"] = ""

    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    if attachments:
        payload["attachment"] = attachments

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": BREVO_API_KEY,
            "accept": "application/json",
            "content-type": "application/json",
            "User-Agent": "FKK-Webbackend/6.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            body = response.read().decode("utf-8")
            result = json.loads(body) if body else {}
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Brevo API Fehler HTTP {response.status}")
            return result
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            detail = ""
        raise RuntimeError(f"Brevo API Fehler HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Brevo API nicht erreichbar: {exc.reason}") from exc


def send_admin_mail(data, pdf_bytes, application_no):
    body = (
        "Ein neuer Mitgliedsantrag ist eingegangen.\n\n"
        f"Name: {data['vorname']} {data['nachname']}\n"
        f"Mitgliedschaft: {data['art']}\n"
        f"E-Mail: {data['email']}\n"
        f"Telefon: {data['telefon']}\n"
        f"Antragsnummer: {application_no}\n\n"
        "Der ausgefüllte Antrag befindet sich als PDF im Anhang."
    )

    attachment = {
        "name": f"FKK_Aufnahmeantrag_{application_no}.pdf",
        "content": base64.b64encode(pdf_bytes).decode("ascii"),
    }

    return brevo_send(
        to=INTERNAL_RECIPIENT,
        subject=f"Neuer Mitgliedsantrag - {data['vorname']} {data['nachname']} - {application_no}",
        text=body,
        reply_to=data["email"],
        attachments=[attachment],
        recipient_name="FKK Mitgliederverwaltung",
    )


def send_member_mail(data, pdf_bytes, application_no):
    welcome_html = load_welcome_html()

    attachment = {
        "name": f"FKK_Aufnahmeantrag_{application_no}.pdf",
        "content": base64.b64encode(pdf_bytes).decode("ascii"),
    }

    return brevo_send(
        to=data["email"],
        subject="Herzlich willkommen beim FKK!",
        html=welcome_html,
        reply_to=REPLY_TO,
        attachments=[attachment],
        recipient_name=f"{data['vorname']} {data['nachname']}",
    )



def send_contact_mail(data):
    body = (
        "Eine neue Nachricht über das Kontaktformular von fkkwesel.de ist eingegangen.\n\n"
        f"Name: {data['name']}\n"
        f"E-Mail: {data['email']}\n"
        f"Betreff: {data['subject']}\n\n"
        "Nachricht:\n"
        f"{data['message']}\n\n"
        "Hinweis: Antworten auf diese E-Mail gehen direkt an den Absender."
    )

    return brevo_send(
        to=INTERNAL_RECIPIENT,
        subject=f"Kontakt über fkkwesel.de – {data['subject']}",
        text=body,
        reply_to=data["email"],
        recipient_name="FKK Kontakt",
    )


@app.get("/welcome")
def welcome_page():
    try:
        html = load_welcome_html()
        return Response(html, mimetype="text/html")
    except FileNotFoundError as exc:
        return Response(
            "<!doctype html><html lang='de'><body style='font-family:Arial;padding:30px'>"
            "<h2>Willkommensseite noch nicht eingerichtet</h2>"
            "<p>Die Datei welcome_email.html fehlt noch im Backend.</p>"
            "</body></html>",
            status=500,
            mimetype="text/html"
        )

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "mail_provider": "brevo",
        "brevo_configured": bool(BREVO_API_KEY),
        "brevo_from_email": BREVO_FROM_EMAIL,
        "welcome_template_present": os.path.exists(
            os.path.join(os.path.dirname(__file__), WELCOME_TEMPLATE)
        ),
    })


@app.post("/api/kontakt")
def submit_contact():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if not check_contact_rate_limit(ip):
        return jsonify({"ok": False, "error": "Zu viele Nachrichten. Bitte später erneut versuchen."}), 429

    payload = request.get_json(silent=True) or {}

    # Honeypot gegen einfache Bots.
    if clean(payload.get("website"), 200):
        return jsonify({"ok": True, "message": "Danke."}), 200

    data = {
        "name": clean(payload.get("name"), 120),
        "email": clean(payload.get("email"), 160).lower(),
        "subject": clean(payload.get("subject"), 180),
        "message": clean(payload.get("message"), 5000),
        "privacy": bool(payload.get("privacy")),
    }

    if not data["name"] or not data["subject"] or not data["message"] or not valid_email(data["email"]) or not data["privacy"]:
        return jsonify({
            "ok": False,
            "error": "Bitte prüfe alle Pflichtfelder und die Datenschutzbestätigung."
        }), 400

    try:
        send_contact_mail(data)
    except Exception:
        # Keine Nachrichtendetails oder personenbezogenen Daten ins Log schreiben.
        app.logger.exception("Versand der Kontaktformular-Nachricht über Brevo fehlgeschlagen.")
        return jsonify({
            "ok": False,
            "error": "Die Nachricht konnte gerade nicht versendet werden. Bitte später erneut versuchen."
        }), 502

    return jsonify({
        "ok": True,
        "message": "Vielen Dank! Deine Nachricht wurde erfolgreich übermittelt."
    }), 200


@app.post("/api/antrag")
def submit_application():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if not check_rate_limit(ip):
        return jsonify({"ok": False, "error": "Zu viele Anfragen. Bitte später erneut versuchen."}), 429

    payload = request.get_json(silent=True) or {}

    # Honeypot: normale Nutzer sehen dieses Feld nicht.
    if clean(payload.get("website"), 200):
        return jsonify({"ok": True, "message": "Danke."}), 200

    data = {
        "art": clean(payload.get("art"), 20),
        "vorname": clean(payload.get("vorname"), 80),
        "nachname": clean(payload.get("nachname"), 100),
        "adresse": clean(payload.get("adresse"), 250),
        "telefon": clean(payload.get("telefon"), 80),
        "email": clean(payload.get("email"), 160).lower(),
        "geburt": clean(payload.get("geburt"), 20),
        "ehe": clean(payload.get("ehe"), 20),
        "iban": clean(payload.get("iban"), 50),
        "sepa": bool(payload.get("sepa")),
        "zeremonie": bool(payload.get("zeremonie")),
        "privacyread": bool(payload.get("privacyread")),
        "finalok": bool(payload.get("finalok")),
    }

    errors = []
    if data["art"] not in ("Aktiv", "Passiv"):
        errors.append("Mitgliedschaft")
    for field in ("vorname", "nachname", "adresse", "telefon", "email", "geburt", "iban"):
        if not data[field]:
            errors.append(field)
    if not valid_email(data["email"]):
        errors.append("E-Mail")
    if not valid_iban(data["iban"]):
        errors.append("IBAN")
    if not all([data["sepa"], data["zeremonie"], data["privacyread"], data["finalok"]]):
        errors.append("Bestätigungen")

    if errors:
        return jsonify({
            "ok": False,
            "error": "Bitte prüfe die Pflichtfelder und Bestätigungen.",
            "fields": sorted(set(errors))
        }), 400

    application_no = f"FKK-{datetime.now():%Y%m%d}-{secrets.token_hex(3).upper()}"
    pdf_bytes = make_application_pdf(data, application_no)

    try:
        send_admin_mail(data, pdf_bytes, application_no)
    except Exception:
        # Keine personenbezogenen Daten oder IBAN ins Log schreiben.
        app.logger.exception("Versand des internen Mitgliedsantrags über Brevo fehlgeschlagen.")
        return jsonify({
            "ok": False,
            "error": "Der Antrag konnte technisch nicht versendet werden. Bitte versuche es später erneut."
        }), 502

    member_mail_sent = True
    warning = ""
    try:
        send_member_mail(data, pdf_bytes, application_no)
    except FileNotFoundError as exc:
        member_mail_sent = False
        warning = str(exc)
        app.logger.error("Welcome template missing: %s", exc)
    except Exception:
        member_mail_sent = False
        warning = "Der Antrag ist eingegangen, aber die Willkommensmail konnte nicht versendet werden."
        app.logger.exception("Willkommensmail über Brevo fehlgeschlagen.")

    response = {
        "ok": True,
        "application_no": application_no,
        "member_mail_sent": member_mail_sent,
        "message": "Dein Antrag wurde erfolgreich übermittelt."
    }
    if member_mail_sent:
        response["message"] += " Die Willkommensmail wurde versendet."
    else:
        response["warning"] = warning

    return jsonify(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
