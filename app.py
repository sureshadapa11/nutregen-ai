# app.py
import io
from flask import send_file
from xhtml2pdf import pisa
import os
import json
import csv
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, jsonify
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from datetime import datetime, timezone


# ---------------------------
# App setup
# ---------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB max upload

def is_allowed(filename):
    return filename.lower().endswith(".txt")

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")  # set on Render
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Ensure folders exist
os.makedirs("uploads", exist_ok=True)

# ---------------------------
# Config / Environment
# ---------------------------
BASE_URL        = os.environ.get("BASE_URL", "http://127.0.0.1:5000")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY")  # set on Render
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "no-reply@nutregenai.com")

# ---------------------------
# Simple "user store"
# ---------------------------
USERS_FILE = "users.json"

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(data):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def set_user_password(email, password, name=None):
    users = load_users()
    key = (email or "").lower().strip()
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
    key = (email or "").lower().strip()
    rec = users.get(key)
    if not rec or "password_hash" not in rec:
        return False
    return check_password_hash(rec["password_hash"], password)

# ---------------------------
# Password reset helpers
# ---------------------------
serializer = URLSafeTimedSerializer(app.secret_key)

def make_reset_token(email):
    return serializer.dumps({"email": email})

def verify_reset_token(token, max_age=3600):  # 1 hour
    try:
        data = serializer.loads(token, max_age=max_age)
        return data.get("email")
    except (BadSignature, SignatureExpired):
        return None

def send_reset_email_via_resend(to_email, reset_link):
    if not RESEND_API_KEY:
        print("WARNING: RESEND_API_KEY not set; skipping email send.")
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
        print("RESEND STATUS:", resp.status_code)
        try:
            print("RESEND BODY:", resp.text[:500])
        except Exception:
            pass
        return resp.status_code in (200, 201, 202)
    except Exception as e:
        print("Resend exception:", e)
        return False


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

# ---------------------------
# Web pages
# ---------------------------

def html_to_pdf_bytes(html_str: str) -> bytes:
    """Render HTML to PDF bytes using xhtml2pdf."""
    out = io.BytesIO()
    # pisa expects a file-like object for src
    pisa_status = pisa.CreatePDF(io.StringIO(html_str), dest=out)
    if pisa_status.err:
        raise RuntimeError("PDF generation failed")
    return out.getvalue()
@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    """
    Accepts the same fields the result page already has and returns a PDF download.
    """
    # Collect fields (posted by the button on result.html)
    name = request.form.get("name", "")
    email = request.form.get("email", "")
    activity = request.form.get("activity", "")
    traits = request.form.getlist("traits")  # multiple
    calories = request.form.get("calories", "")
    # macros as numbers
    macros = {
        "carbs": int(request.form.get("macros_carbs", "0") or 0),
        "fats": int(request.form.get("macros_fats", "0") or 0),
        "protein": int(request.form.get("macros_protein", "0") or 0),
    }

    # meal plan (sent as JSON text)
    try:
        plan_json = request.form.get("plan_json", "[]")
        plan = json.loads(plan_json)
    except Exception:
        plan = []

    dna_summary = request.form.get("dna_summary", "")

    # Render HTML for the PDF
    html = render_template(
        "pdf_template.html",
        name=name,
        email=email,
        activity=activity,
        traits=traits,
        calories=calories,
        macros=macros,
        plan=plan,
        dna_summary=dna_summary,
    )

    # Convert to PDF
    pdf_bytes = html_to_pdf_bytes(html)

    # Send as a file download
    filename = f"NutreGenAI_Plan_{name or 'client'}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/")
def home():
    # If your Intro/Login lives on Webador, this can redirect to form
    return redirect(url_for("form_page"))

@app.route("/form", methods=["GET"])
def form_page():
    # Show your DNA form (templates/index.html)
    # Make sure index.html has fields: name, email, activity, traits[], dna (file)
    return render_template("index.html")

# ---------------------------
# DNA form handler (no OpenAI)
# ---------------------------
@app.route("/generate", methods=["POST"])
def generate_plan():
    """
    Handles form POST from Webador or local /form.
    Expects: name, email, activity, optional traits (multiple), file 'dna' (.txt)
    Returns: result.html with a simple generated plan (MVP)
    """
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    activity = request.form.get("activity", "").strip()
    traits = request.form.getlist("traits")  # may be []
    dna_file = request.files.get("dna")

    if not name or not email or not activity or not dna_file:
        return "Missing required fields.", 400
    if not is_allowed(dna_file.filename):
        return "Only .txt DNA files are allowed.", 400

    # Save upload
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_email = email.replace("@", "_at_").replace(".", "_")
    filename = f"{safe_email}_{ts}.txt"
    upload_path = os.path.join("uploads", filename)
    dna_file.save(upload_path)

    # Basic parsing of DNA file (MVP: count lines/size)
    try:
        with open(upload_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        dna_summary = f"File lines: {len(lines)}"
    except Exception:
        dna_summary = "File processed."

    # Simple nutrition logic (MVP)
    base_calories = {"Low": 1800, "Moderate": 2200, "High": 2600}.get(activity, 2000)
    # Adjust based on traits
    carb_bias = -0.05 if "High Carb Sensitivity" in traits else 0.0
    fat_bias = -0.05 if "Slow Fat Metabolism" in traits else 0.0
    protein_bias = 0.05 if ("High Carb Sensitivity" in traits or "Slow Fat Metabolism" in traits) else 0.0

    carbs = max(0.35 + carb_bias, 0.20)   # fraction of calories
    fats = max(0.30 + fat_bias, 0.20)
    protein = max(0.35 + protein_bias, 0.25)
    # Normalize (keep it simple)
    total = carbs + fats + protein
    carbs, fats, protein = carbs/total, fats/total, protein/total

    # Sample meal plan (very simple placeholder)
    plan = [
        {"meal": "Breakfast", "idea": "Greek yogurt with berries and chia", "notes": "High protein"},
        {"meal": "Lunch",     "idea": "Grilled chicken salad + olive oil",  "notes": "Balanced fats"},
        {"meal": "Snack",     "idea": "Apple + handful of almonds",         "notes": "Fiber + fats"},
        {"meal": "Dinner",    "idea": "Salmon, quinoa, and greens",         "notes": "Protein + complex carbs"}
    ]

    # (Optional) Save to CSV log (dedupe by email+date)
    try:
        csv_path = "submissions.csv"
        exists = os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["timestamp", "name", "email", "activity", "traits", "file"])
            writer.writerow([datetime.now(timezone.utc).isoformat(), name, email, activity, "|".join(traits), filename])
    except Exception as e:
        print("CSV log error:", e)

    # Render result page (make sure templates/result.html uses these vars)
    return render_template(
        "result.html",
        name=name,
        email=email,
        activity=activity,
        traits=traits,
        calories=base_calories,
        macros={"carbs": round(carbs*100), "fats": round(fats*100), "protein": round(protein*100)},
        plan=plan,
        dna_summary=dna_summary
    )

# ---------------------------
# Password reset routes
# ---------------------------

@app.route("/api/password-reset", methods=["POST"])
def api_password_reset():
    """
    Expects JSON: { "email": "user@example.com" }
    Always returns {ok: true} to avoid leaking whether a user exists.
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"ok": True})
    token = make_reset_token(email)
    reset_link = f"{BASE_URL}/reset?token={token}"
    print("RESET LINK:", reset_link)
    send_reset_email_via_resend(email, reset_link)
    return jsonify({"ok": True})

@app.route("/reset")
def reset_page():
    """
    Renders your templates/reset.html with token + validity.
    """
    token = request.args.get("token", "")
    email = verify_reset_token(token)
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

    email = verify_reset_token(token)
    if not email:
        return jsonify({"ok": False, "error": "invalid_or_expired"}), 400

    set_user_password(email, new_pw)
    return jsonify({"ok": True})

# ---------------------------
# Health
# ---------------------------
@app.route("/healthz")
def healthz():
    return "ok", 200

# ---------------------------
# Run (Render binds PORT)
# ---------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)

@app.route("/diag/send-test")
def diag_send_test():
    # Mask the API key so it’s safe in logs
    masked = (RESEND_API_KEY[:6] + "..." + RESEND_API_KEY[-4:]) if RESEND_API_KEY else "MISSING"
    to = request.args.get("to") or "youremail@example.com"
    link = f"{BASE_URL}/reset?token=test"
    ok = send_reset_email_via_resend(to, link)
    return jsonify({
        "resend_api_key_present": bool(RESEND_API_KEY),
        "resend_api_key_masked": masked,
        "from_email": FROM_EMAIL,
        "base_url": BASE_URL,
        "sent_ok": ok
    })

