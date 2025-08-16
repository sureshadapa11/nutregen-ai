# app.py
import os
import io
import csv
import json
import base64
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, jsonify, send_file, abort
from flask_cors import CORS

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

# Email (Resend)
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Folders
os.makedirs("uploads", exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs("uploads/results", exist_ok=True)

CSV_LOG = "submissions.csv"

# Email config (set env vars on Render)
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY")  # required for real emails
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "no-reply@nutregenai.com")
BASE_URL        = os.environ.get("BASE_URL", "http://127.0.0.1:5000")

# ---------- Helpers ----------

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

        # Diet
        if diet and diet != "regular":
            if diet not in tags_l:
                continue

        # Allergies (basic keyword check)
        blocked = False
        for a in allergies:
            a_l = a.lower()
            if a_l in idea_l:
                blocked = True
                break
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
                "sleep_hours","stress","water","budget","cook_time","prefs","traits","filename","dna_lines","rid"
            ])
        w.writerow(row)

def save_result_payload(payload):
    """Save a single calculation to uploads/results/<rid>.json and return rid."""
    rid = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    out_path = os.path.join("uploads", "results", f"{rid}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return rid

def load_result_payload(rid):
    path = os.path.join("uploads", "results", f"{rid}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------- PDF ----------

def build_pdf_bytes(data):
    """Generate a simple PDF from the result payload and return bytes."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1*inch, height - 1*inch, "NutreGen AI — Personalised Nutrition Plan")

    y = height - 1.3*inch
    def line(txt, font="Helvetica", size=11, gap=14):
        nonlocal y
        if y < 1*inch:
            c.showPage(); y = height - 1*inch; c.setFont(font, size)
        else:
            c.setFont(font, size)
        c.drawString(1*inch, y, txt)
        y -= gap

    # Header info
    line(f"Name: {data.get('name','')}")
    line(f"Email: {data.get('email','')}")
    line(f"Activity: {data.get('activity','')}")
    line(f"DNA summary: {data.get('dna_summary','')}")
    line("")

    # Profile
    line("Profile", "Helvetica-Bold", 12, 18)
    line(f"Goal: {data.get('goal','-')}")
    line(f"Diet: {data.get('diet','-')}")
    allergies = ", ".join(data.get("allergies", [])) or "None"
    prefs = ", ".join(data.get("prefs", [])) or "—"
    line(f"Allergies: {allergies}")
    line(f"Preferences: {prefs}")
    line(f"Sleep (hrs): {data.get('sleep_hours','-')}")
    line(f"Stress: {data.get('stress','-')}")
    line(f"Water: {data.get('water','-')}")
    line(f"Budget: {data.get('budget','-')} | Cook time: {data.get('cook_time','-')}")
    line("")

    # Targets
    line("Targets", "Helvetica-Bold", 12, 18)
    macros = data.get("macros", {})
    line(f"Calories (daily): {data.get('calories','')}")
    line(f"Macros: Carbs {macros.get('carbs','-')}% | Fats {macros.get('fats','-')}% | Protein {macros.get('protein','-')}%")
    line("")

    # Meals
    line("Meal Ideas", "Helvetica-Bold", 12, 18)
    for item in data.get("plan", []):
        line(f"- {item.get('meal')}: {item.get('idea')} ({item.get('notes')})")

    c.save()
    buf.seek(0)
    return buf.read()

def send_email_with_pdf(to_email, subject, html, pdf_bytes, filename="NutreGenAI_Plan.pdf"):
    """Send email with PDF attachment via Resend. Returns True on success (or in dev if no key)."""
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
                "attachments": [
                    {
                        "filename": filename,
                        "content": b64
                    }
                ],
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

# ---------- Routes ----------

@app.route("/")
def home():
    return redirect(url_for("form_page"))

@app.route("/form", methods=["GET"])
def form_page():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate_plan_html():
    # Required
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

    # Save upload
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

    # Compute
    calories, macros = compute_plan(activity, traits, goal, sleep_hrs, stress)
    plan = filter_meals(diet, allergies, prefs, cook_time, budget)

    # Save payload -> rid for PDF/email
    payload = {
        "name": name, "email": email, "activity": activity, "traits": traits,
        "goal": goal, "diet": diet, "allergies": allergies, "sleep_hours": sleep_hrs,
        "stress": stress, "water": water, "budget": budget, "cook_time": cook_time,
        "calories": calories, "macros": macros, "plan": plan, "dna_summary": dna_summary
    }
    rid = save_result_payload(payload)

    # Log
    try:
        log_csv([
            datetime.utcnow().isoformat(), name, email, activity, goal, diet,
            "|".join(allergies), sleep_hrs, stress, water, budget, cook_time,
            "|".join(prefs), "|".join(traits), filename, dna_lines, rid
        ])
    except Exception as e:
        print("CSV log error:", e)

    return render_template(
        "result.html",
        rid=rid,
        name=name, email=email, activity=activity, traits=traits,
        goal=goal, diet=diet, allergies=allergies, sleep_hours=sleep_hrs,
        stress=stress, water=water, budget=budget, cook_time=cook_time,
        calories=calories, macros=macros, plan=plan, dna_summary=dna_summary
    )

# JSON endpoint for mobile
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

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_email = email.replace("@","_at_").replace(".","_")
    filename = f"{safe_email}_{ts}.txt"
    path = os.path.join("uploads", filename)
    dna_file.save(path)

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        dna_summary = f"File lines: {len(lines)}"
        dna_lines = len(lines)
    except Exception:
        dna_summary = "File processed."
        dna_lines = ""

    calories, macros = compute_plan(activity, traits, goal, sleep_hrs, stress)
    plan = filter_meals(diet, allergies, prefs, cook_time, budget)

    payload = {
        "name": name, "email": email, "activity": activity, "traits": traits,
        "goal": goal, "diet": diet, "allergies": allergies, "sleep_hours": sleep_hrs,
        "stress": stress, "water": water, "budget": budget, "cook_time": cook_time,
        "calories": calories, "macros": macros, "plan": plan, "dna_summary": dna_summary
    }
    rid = save_result_payload(payload)

    try:
        log_csv([
            datetime.utcnow().isoformat(), name, email, activity, goal, diet,
            "|".join(allergies), sleep_hrs, stress, water, budget, cook_time,
            "|".join(prefs), "|".join(traits), filename, dna_lines, rid
        ])
    except Exception as e:
        print("CSV log error:", e)

    return jsonify({"ok": True, "rid": rid, **payload}), 200

# ---------- PDF & Email routes ----------

@app.route("/download-pdf")
def download_pdf():
    rid = request.args.get("id") or request.args.get("rid")
    if not rid:
        return abort(400, "Missing id")
    data = load_result_payload(rid)
    if not data:
        return abort(404, "Not found")

    pdf_bytes = build_pdf_bytes(data)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="NutreGenAI_Plan.pdf"
    )

@app.route("/email-plan")
def email_plan():
    rid = request.args.get("id") or request.args.get("rid")
    if not rid:
        return abort(400, "Missing id")
    data = load_result_payload(rid)
    if not data:
        return abort(404, "Not found")

    to_email = data.get("email")
    if not to_email:
        return abort(400, "Missing email in record")

    pdf_bytes = build_pdf_bytes(data)
    subject = "Your NutreGen AI Plan"
    html = f"""
    <p>Hello {data.get('name','')},</p>
    <p>Attached is your personalised NutreGen AI nutrition plan.</p>
    <p>You can also download it here: <a href="{BASE_URL}/download-pdf?id={rid}">{BASE_URL}/download-pdf?id={rid}</a></p>
    <p>— NutreGen AI</p>
    """
    ok = send_email_with_pdf(to_email, subject, html, pdf_bytes)
    if ok:
        return render_template("email_sent.html", email=to_email)
    return "Email failed to send. Please try again later.", 500

@app.route("/healthz")
def healthz():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
