# app.py
import os
import io
import csv
import json
import base64
from datetime import datetime
from flask import (
    Flask, request, render_template, redirect, url_for,
    jsonify, send_file, abort
)
from flask_cors import CORS

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

# Email (Resend)
import requests

# Tokens
from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer, BadSignature, SignatureExpired

# Password hashing for reset flow (simple file-based user store)
from werkzeug.security import generate_password_hash, check_password_hash

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")  # set on Render
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Folders
os.makedirs("uploads", exist_ok=True)
os.makedirs("templates", exist_ok=True)

CSV_LOG = "submissions.csv"
USERS_FILE = "users.json"

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")  # set on Render for real emails
FROM_EMAIL     = os.environ.get("FROM_EMAIL", "no-reply@nutregenai.com")
BASE_URL       = os.environ.get("BASE_URL", "http://127.0.0.1:5000")

# Stateless token for PDF/email (no DB/filesystem dependency)
PDF_SIGNER   = URLSafeSerializer(app.secret_key, salt="nutregen-pdf-v1")
# Timed token for password reset (expires)
RESET_SIGNER = URLSafeTimedSerializer(app.secret_key, salt="nutregen-reset-v1")

# -----------------------------------------------------------------------------
# Simple user store (for password reset demo)
# -----------------------------------------------------------------------------
def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(data):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def set_user_password(email, password, name=None):
    users = load_users()
    key = (email or "").strip().lower()
    if not key:
        return
    rec = users.get(key, {})
    if name:
        rec["name"] = name
    rec["password_hash"] = generate_password_hash(password)
    users[key] = rec
    save_users(users)

def verify_user(email, password):
    users = load_users()
    key = (email or "").strip().lower()
    rec = users.get(key)
    if not rec or "password_hash" not in rec:
        return False
    return check_password_hash(rec["password_hash"], password)

# -----------------------------------------------------------------------------
# Helpers for nutrition plan
# -----------------------------------------------------------------------------
def parse_float(val, default=7.0):
    try:
        return float(val)
    except Exception:
        return default

MEALS = [
    ("Breakfast", "Greek yogurt with berries & chia", "High protein", ["vegetarian"]),
    ("Breakfast", "Tofu scramble with spinach", "Vegan protein", ["vegan"]),
    ("Breakfast", "Overnight oats + flax + berries", "Budget, <15m", ["vegan","vegetarian"]),
    ("Lunch", "Grilled chicken salad + olive oil", "Balanced fats", ["regular","low-carb"]),
    ("Lunch", "Chickpea & quinoa bowl", "High fibre", ["vegan","vegetarian"]),
    ("Lunch", "Tuna & mixed bean salad", "Pescatarian, <15m", ["pescatarian"]),
    ("Snack", "Apple + almonds", "Fiber + fats", ["vegan","vegetarian","regular"]),
    ("Snack", "Banana + peanut butter", "Budget, <5m", ["vegan","vegetarian","regular"]),
    ("Dinner", "Salmon, quinoa, and greens", "Protein + complex carbs", ["pescatarian","regular"]),
    ("Dinner", "Lentil curry + brown rice", "Plant protein", ["vegan","vegetarian","indian"]),
    ("Dinner", "Paneer tikka + salad", "Vegetarian, high protein", ["vegetarian","indian"]),
    ("Dinner", "Chicken stir-fry + veg + rice", "Quick, balanced", ["regular"]),
]

def filter_meals(diet, allergies, prefs, cook_time, budget):
    out = []
    for meal, idea, notes, tags in MEALS:
        idea_l = idea.lower()
        tags_l = [t.lower() for t in tags]

        # Diet filter
        if diet and diet != "regular":
            if diet not in tags_l:
                continue

        # Very simple allergy screening
        blocked = False
        for a in allergies:
            a_l = a.lower()
            if a_l in idea_l:
                blocked = True; break
            if a_l == "dairy" and any(x in idea_l for x in ["yogurt","paneer","cheese","milk"]):
                blocked = True; break
            if a_l == "eggs" and "egg" in idea_l:
                blocked = True; break
            if a_l == "shellfish" and any(x in idea_l for x in ["prawn","shrimp","crab","lobster"]):
                blocked = True; break
            if a_l == "soy" and "tofu" in idea_l:
                blocked = True; break
            if a_l == "gluten" and any(x in idea_l for x in ["bread","wrap","pasta","wheat"]):
                blocked = True; break
            if a_l == "nuts" and any(x in idea_l for x in ["almond","peanut","cashew","walnut","nut"]):
                blocked = True; break
        if blocked:
            continue

        # Preferences (soft filter)
        if prefs:
            pref_match = any((p.lower() in tags_l) or (p.lower() in idea_l) for p in prefs)
            if not pref_match:
                continue

        final_notes = notes
        if cook_time == "<15" and "Budget" not in final_notes:
            final_notes += " • ~<20m"
        if budget == "£" and "Budget" not in final_notes:
            final_notes += " • Budget"

        out.append({"meal": meal, "idea": idea, "notes": final_notes})
        if len(out) >= 4:
            break

    if len(out) < 4:
        out = [
            {"meal":"Breakfast","idea":"Overnight oats + flax + berries","notes":"<15m, budget-friendly"},
            {"meal":"Lunch","idea":"Bean & veg wrap","notes":"Quick, high fibre"},
            {"meal":"Snack","idea":"Banana + peanut butter","notes":"Budget, <5m"},
            {"meal":"Dinner","idea":"Stir-fry tofu & veg + rice","notes":"<20m, vegan/vegetarian"}
        ]
    return out

def compute_plan(activity, traits, goal, sleep_hours, stress):
    base = {"Low": 1800, "Moderate": 2200, "High": 2600}.get(activity, 2000)
    if goal == "loss":
        base -= 250
    elif goal == "gain":
        base += 250

    carb_bias = -0.05 if "High Carb Sensitivity" in traits else 0.0
    fat_bias  = -0.05 if "Slow Fat Metabolism" in traits else 0.0
    protein_bias = 0.05 if ("High Carb Sensitivity" in traits or "Slow Fat Metabolism" in traits) else 0.0

    sh = parse_float(sleep_hours, 7.0)
    if sh < 6:
        protein_bias += 0.05

    stress_carb_delta = -0.05 if stress == "high" else 0.0

    carbs   = max(0.35 + carb_bias + stress_carb_delta, 0.20)
    fats    = max(0.30 + fat_bias, 0.20)
    protein = max(0.35 + protein_bias, 0.25)
    total = carbs + fats + protein
    carbs, fats, protein = carbs/total, fats/total, protein/total
    macros = {"carbs": round(carbs*100), "fats": round(fats*100), "protein": round(protein*100)}
    return base, macros

def log_csv(row):
    exists = os.path.exists(CSV_LOG)
    with open(CSV_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow([
                "timestamp","name","email","activity","goal","diet","allergies",
                "sleep_hours","stress","water","budget","cook_time","prefs","traits","filename","dna_lines"
            ])
        w.writerow(row)

# -----------------------------------------------------------------------------
# PDF generation + email
# -----------------------------------------------------------------------------
def build_pdf_bytes(data):
    buf = io.BytesIO()

    # Document & margins
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=1.7*cm, bottomMargin=1.7*cm,
        title="NutreGen AI — Personalised Nutrition Plan",
        author="NutreGen AI"
    )

    # ---------- Styles ----------
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=18, textColor=colors.HexColor("#2e7d32"), spaceAfter=8))
    styles.add(ParagraphStyle(name="H2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=14, textColor=colors.HexColor("#2e7d32"), spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="Body", parent=styles["BodyText"], fontSize=10.5, leading=14))
    styles.add(ParagraphStyle(name="Muted", parent=styles["BodyText"], fontSize=9, textColor=colors.HexColor("#6a8f6b")))
    styles.add(ParagraphStyle(name="Label", parent=styles["BodyText"], fontSize=10.5, textColor=colors.HexColor("#2f5530")))
    styles.add(ParagraphStyle(name="KPI", parent=styles["BodyText"], fontSize=11.5, leading=14, textColor=colors.HexColor("#1b5e20")))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8.5, textColor=colors.grey))

    # ---------- Elements ----------
    E = []

    # Header
    E.append(Paragraph("NutreGen AI — Personalised Nutrition Plan", styles["H1"]))
    sub = f"{data.get('name','')} &bull; {data.get('email','')} &bull; {data.get('activity','')}"
    E.append(Paragraph(sub, styles["Muted"]))
    E.append(Spacer(1, 6))
    E.append(HRFlowable(color=colors.HexColor("#e0eee3"), width="100%", thickness=1))
    E.append(Spacer(1, 10))

    # DNA summary / info grid
    info = [
        ["DNA summary", data.get("dna_summary", "—")],
        ["Goal", (data.get("goal") or "—").title() if data.get("goal") else "—"],
        ["Diet", (data.get("diet") or "—").title() if data.get("diet") else "—"],
    ]
    allergies = ", ".join(data.get("allergies", [])) or "None"
    prefs = ", ".join(data.get("prefs", [])) or "—"
    info += [
        ["Allergies", allergies],
        ["Preferences", prefs],
        ["Sleep (hrs)", str(data.get("sleep_hours") or "—")],
        ["Stress", (data.get("stress") or "—").title() if data.get("stress") else "—"],
        ["Water", data.get("water") or "—"],
        ["Budget / Cook time", f"{data.get('budget','—')} / {data.get('cook_time','—')}"],
    ]

    t_info = Table(info, colWidths=[4.0*cm, None])
    t_info.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f3fbf4")),
        ("TEXTCOLOR",  (0,0), (-1,0), colors.HexColor("#2e7d32")),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("INNERGRID",  (0,0), (-1,-1), 0.25, colors.HexColor("#e6efe8")),
        ("BOX",        (0,0), (-1,-1), 0.5, colors.HexColor("#e6efe8")),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("FONTNAME",   (0,1), (0,-1), "Helvetica-Bold"),
        ("TEXTCOLOR",  (0,1), (0,-1), colors.HexColor("#2f5530")),
        ("LEFTPADDING",(0,0), (-1,-1), 6),
        ("RIGHTPADDING",(0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0), (-1,-1), 6),
    ]))
    E.append(t_info)
    E.append(Spacer(1, 12))

    # Targets (Calories + Macros)
    E.append(Paragraph("Daily Targets", styles["H2"]))
    macros = data.get("macros", {})
    kpi = [
        ["Calories (kcal)", str(data.get("calories","—"))],
        ["Carbs (%)",       str(macros.get("carbs","—"))],
        ["Fats (%)",        str(macros.get("fats","—"))],
        ["Protein (%)",     str(macros.get("protein","—"))],
    ]
    t_kpi = Table(kpi, colWidths=[5.0*cm, None])
    t_kpi.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#2e7d32")),
        ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1),
            [colors.HexColor("#f9fffb"), colors.HexColor("#f3fbf4")]),
        ("INNERGRID",  (0,0), (-1,-1), 0.25, colors.HexColor("#d9eadf")),
        ("BOX",        (0,0), (-1,-1), 0.5, colors.HexColor("#d9eadf")),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING",(0,0), (-1,-1), 6),
        ("RIGHTPADDING",(0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0), (-1,-1), 6),
    ]))
    E.append(t_kpi)
    E.append(Spacer(1, 12))

    # Meal ideas (table with zebra striping)
    E.append(Paragraph("Meal Ideas", styles["H2"]))
    meals = [["Meal", "Idea", "Notes"]]
    for item in data.get("plan", []):
        meals.append([item.get("meal",""), item.get("idea",""), item.get("notes","")])
    t_meals = Table(meals, colWidths=[2.8*cm, 9.0*cm, None])
    t_meals.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#2e7d32")),
        ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1),
            [colors.HexColor("#ffffff"), colors.HexColor("#f7fbf8")]),
        ("INNERGRID",  (0,0), (-1,-1), 0.25, colors.HexColor("#d9eadf")),
        ("BOX",        (0,0), (-1,-1), 0.5, colors.HexColor("#d9eadf")),
        ("VALIGN",     (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",(0,0), (-1,-1), 6),
        ("RIGHTPADDING",(0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0), (-1,-1), 6),
    ]))
    E.append(t_meals)
    E.append(Spacer(1, 10))

    # Footer note
    E.append(Spacer(1, 6))
    E.append(HRFlowable(color=colors.HexColor("#e0eee3"), width="100%", thickness=1))
    E.append(Spacer(1, 6))
    E.append(Paragraph("This plan is generated from your provided DNA & lifestyle inputs for informational purposes.", styles["Small"]))

    # Page footer with brand + page number
    def on_page(canvas, doc_):
        canvas.saveState()
        footer = "NutreGen AI • nutregen.ai"
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#6a8f6b"))
        canvas.drawString(2*cm, 1.0*cm, footer)
        canvas.drawRightString(A4[0]-2*cm, 1.0*cm, f"Page {doc_.page}")
        canvas.restoreState()

    # Build
    doc.build(E, onFirstPage=on_page, onLaterPages=on_page)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


def send_email_with_pdf(to_email, subject, html, pdf_bytes, filename="NutreGenAI_Plan.pdf"):
    if not RESEND_API_KEY:
        print("WARNING: RESEND_API_KEY not set; skipping actual send. Would send to:", to_email)
        return True
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"NutreGen AI <{FROM_EMAIL}>",
                "to": [to_email],
                "subject": subject,
                "html": html,
                "attachments": [{ "filename": filename, "content": b64 }],
            },
            timeout=20,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            print("Resend error:", resp.status_code, resp.text)
        return ok
    except Exception as e:
        print("Resend exception:", e)
        return False

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def home():
    return redirect(url_for("form_page"))

@app.route("/form", methods=["GET"])
def form_page():
    # Your Webador page posts directly to /generate — this local page is handy for testing
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate_plan():
    """
    Handles form POST from Webador or local /form.
    Expects: name, email, activity, traits[], dna (.txt) and lifestyle fields.
    Renders result.html with tokenized PDF/email links.
    """
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    activity = (request.form.get("activity") or "").strip()
    dna_file = request.files.get("dna")

    if not name or not email or not activity or not dna_file:
        return "Missing required fields.", 400
    if not dna_file.filename.lower().endswith(".txt"):
        return "Only .txt DNA files are allowed.", 400

    # Lifestyle fields
    goal       = (request.form.get("goal") or "").strip()
    diet       = (request.form.get("diet") or "regular").strip()
    allergies  = request.form.getlist("allergies")
    sleep_hrs  = (request.form.get("sleep_hours") or "").strip()
    stress     = (request.form.get("stress") or "").strip()
    water      = (request.form.get("water") or "").strip()
    budget     = (request.form.get("budget") or "").strip()
    cook_time  = (request.form.get("cook_time") or "").strip()
    prefs      = request.form.getlist("prefs")
    traits     = request.form.getlist("traits")

    # Save upload (audit only)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_email = email.replace("@","_at_").replace(".","_")
    filename = f"{safe_email}_{ts}.txt"
    path = os.path.join("uploads", filename)
    dna_file.save(path)

    # DNA summary
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        dna_summary = f"File lines: {len(lines)}"
        dna_lines = len(lines)
    except Exception:
        dna_summary = "File processed."
        dna_lines = ""

    # Compute plan
    calories, macros = compute_plan(activity, traits, goal, sleep_hrs, stress)
    plan = filter_meals(diet, allergies, prefs, cook_time, budget)

    # CSV log (optional analytics)
    try:
        log_csv([
            datetime.utcnow().isoformat(), name, email, activity, goal, diet,
            "|".join(allergies), sleep_hrs, stress, water, budget, cook_time,
            "|".join(prefs), "|".join(traits), filename, dna_lines
        ])
    except Exception as e:
        print("CSV log error:", e)

    # Build token (stateless)
    payload = {
        "name": name, "email": email, "activity": activity, "traits": traits,
        "goal": goal, "diet": diet, "allergies": allergies, "sleep_hours": sleep_hrs,
        "stress": stress, "water": water, "budget": budget, "cook_time": cook_time,
        "calories": calories, "macros": macros, "plan": plan, "dna_summary": dna_summary
    }
    token = PDF_SIGNER.dumps(payload)

    # Render result page with buttons
    return render_template(
        "result.html",
        token=token,
        name=name, email=email, activity=activity, traits=traits,
        goal=goal, diet=diet, allergies=allergies, sleep_hours=sleep_hrs,
        stress=stress, water=water, budget=budget, cook_time=cook_time,
        calories=calories, macros=macros, plan=plan, dna_summary=dna_summary
    )

# JSON variant (for mobile)
@app.route("/api/generate", methods=["POST"])
def api_generate():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    activity = (request.form.get("activity") or "").strip()
    dna_file = request.files.get("dna")
    if not name or not email or not activity or not dna_file:
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    if not dna_file.filename.lower().endswith(".txt"):
        return jsonify({"ok": False, "error": "bad_filetype"}), 400

    goal       = (request.form.get("goal") or "").strip()
    diet       = (request.form.get("diet") or "regular").strip()
    allergies  = request.form.getlist("allergies")
    sleep_hrs  = (request.form.get("sleep_hours") or "").strip()
    stress     = (request.form.get("stress") or "").strip()
    water      = (request.form.get("water") or "").strip()
    budget     = (request.form.get("budget") or "").strip()
    cook_time  = (request.form.get("cook_time") or "").strip()
    prefs      = request.form.getlist("prefs")
    traits     = request.form.getlist("traits")

    # Save file (audit)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_email = email.replace("@","_at_").replace(".","_")
    filename = f"{safe_email}_{ts}.txt"
    path = os.path.join("uploads", filename)
    dna_file.save(path)

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        dna_summary = f"File lines: {len(lines)}"
    except Exception:
        dna_summary = "File processed."

    calories, macros = compute_plan(activity, traits, goal, sleep_hrs, stress)
    plan = filter_meals(diet, allergies, prefs, cook_time, budget)
    payload = {
        "name": name, "email": email, "activity": activity, "traits": traits,
        "goal": goal, "diet": diet, "allergies": allergies, "sleep_hours": sleep_hrs,
        "stress": stress, "water": water, "budget": budget, "cook_time": cook_time,
        "calories": calories, "macros": macros, "plan": plan, "dna_summary": dna_summary
    }
    token = PDF_SIGNER.dumps(payload)
    return jsonify({"ok": True, "token": token, **payload}), 200

# -----------------------------------------------------------------------------
# PDF & Email (stateless via token)
# -----------------------------------------------------------------------------
@app.route("/download-pdf")
def download_pdf():
    token = request.args.get("token")
    if not token:
        return abort(400, "Missing token")
    try:
        data = PDF_SIGNER.loads(token)
    except BadSignature:
        return abort(400, "Invalid token")

    pdf_bytes = build_pdf_bytes(data)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="NutreGenAI_Plan.pdf"
    )

@app.route("/email-plan")
def email_plan():
    token = request.args.get("token")
    if not token:
        return abort(400, "Missing token")
    try:
        data = PDF_SIGNER.loads(token)
    except BadSignature:
        return abort(400, "Invalid token")

    to_email = data.get("email")
    if not to_email:
        return abort(400, "Missing email in token")

    pdf_bytes = build_pdf_bytes(data)
    subject = "Your NutreGen AI Plan"
    html = f"""
    <p>Hello {data.get('name','')},</p>
    <p>Attached is your personalised NutreGen AI nutrition plan.</p>
    <p>You can also download it here: <a href="{BASE_URL}/download-pdf?token={token}">{BASE_URL}/download-pdf?token={token}</a></p>
    <p>— NutreGen AI</p>
    """
    ok = send_email_with_pdf(to_email, subject, html, pdf_bytes)
    if ok:
        return render_template("email_sent.html", email=to_email)
    return "Email failed to send. Please try again later.", 500

# -----------------------------------------------------------------------------
# Password reset (email-only verification via Resend)
# -----------------------------------------------------------------------------
def send_reset_email_via_resend(to_email, reset_link):
    if not RESEND_API_KEY:
        print("WARNING: RESEND_API_KEY not set; skipping email send.")
        print("RESET LINK:", reset_link)
        return True
    subject = "Reset your NutreGen AI password"
    html = f"""
    <p>Hello,</p>
    <p>We received a request to reset your NutreGen AI password.</p>
    <p>Click the link below to set a new password (valid for 1 hour):</p>
    <p><a href="{reset_link}">{reset_link}</a></p>
    <p>If you didn’t request this, you can ignore this email.</p>
    <p>— NutreGen AI</p>
    """
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"NutreGen AI <{FROM_EMAIL}>",
                "to": [to_email],
                "subject": subject,
                "html": html,
            },
            timeout=15,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            print("Resend error:", resp.status_code, resp.text)
        return ok
    except Exception as e:
        print("Resend exception:", e)
        return False

@app.route("/api/password-reset", methods=["POST"])
def api_password_reset():
    """
    Expects JSON: { "email": "user@example.com" }
    Always returns {ok: true} (no user enumeration).
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": True})
    token = RESET_SIGNER.dumps({"email": email})
    reset_link = f"{BASE_URL}/reset?token={token}"
    print("RESET LINK:", reset_link)
    send_reset_email_via_resend(email, reset_link)
    return jsonify({"ok": True})

@app.route("/reset")
def reset_page():
    """
    Renders templates/reset.html with token + validity.
    """
    token = request.args.get("token", "")
    email = None
    try:
        payload = RESET_SIGNER.loads(token, max_age=3600)  # 1 hour
        email = payload.get("email")
    except (BadSignature, SignatureExpired):
        email = None
    return render_template("reset.html", token=token, valid=bool(email))

@app.route("/api/reset-password", methods=["POST"])
def api_reset_password():
    """
    Expects JSON: { "token": "...", "password": "newpass" }
    Verifies token, then saves new password in users.json
    """
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    new_pw = data.get("password")
    if not token or not new_pw:
        return jsonify({"ok": False, "error": "missing"}), 400
    try:
        payload = RESET_SIGNER.loads(token, max_age=3600)
        email = (payload.get("email") or "").strip()
    except (BadSignature, SignatureExpired):
        return jsonify({"ok": False, "error": "invalid_or_expired"}), 400

    if not email:
        return jsonify({"ok": False, "error": "invalid"}), 400

    set_user_password(email, new_pw)
    return jsonify({"ok": True})

# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    return "ok", 200

# -----------------------------------------------------------------------------
# Run (Render binds PORT)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
