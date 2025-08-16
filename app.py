# app.py
import os
import csv
import json
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, jsonify
from flask_cors import CORS

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Folders
os.makedirs("uploads", exist_ok=True)
os.makedirs("templates", exist_ok=True)

CSV_LOG = "submissions.csv"

# ---------- Helpers ----------

def parse_float(val, default=7.0):
    try:
        return float(val)
    except Exception:
        return default

# Simple meal catalog with tags for filtering
MEALS = [
    # meal, idea, notes, tags (diet tags + style tags)
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
    # Very simple filter: diet tag must match (unless regular), exclude ideas containing allergens,
    # prefer at least one preference match if prefs provided.
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
            # crude exclusions
            if a_l == "dairy" and any(x in idea_l for x in ["yogurt","paneer","cheese","milk"]):
                blocked = True
                break
            if a_l == "eggs" and "egg" in idea_l:
                blocked = True
                break
            if a_l == "shellfish" and any(x in idea_l for x in ["prawn","shrimp","crab","lobster"]):
                blocked = True
                break
            if a_l == "soy" and "tofu" in idea_l:
                blocked = True
                break
            if a_l == "gluten" and any(x in idea_l for x in ["bread","wrap","pasta","wheat"]):
                blocked = True
                break
            if a_l == "nuts" and any(x in idea_l for x in ["almond","peanut","cashew","walnut","nut"]):
                blocked = True
                break
        if blocked:
            continue

        # Preferences (soft filter: if prefs exist, require at least one match in tags or idea text)
        if prefs:
            pref_match = any((p.lower() in tags_l) or (p.lower() in idea_l) for p in prefs)
            if not pref_match:
                continue

        # Cooking time / budget: for now we only mark notes; advanced metadata can refine later
        final_notes = notes
        if cook_time == "<15" and "Budget" not in final_notes:
            final_notes += " • ~<20m"
        if budget == "£" and "Budget" not in final_notes:
            final_notes += " • Budget"

        out.append({"meal": meal, "idea": idea, "notes": final_notes})

        if len(out) >= 4:
            break

    # Fallback to 4 basics if filtering was too strict
    if len(out) < 4:
        out = [
            {"meal":"Breakfast","idea":"Overnight oats + flax + berries","notes":"<15m, budget-friendly"},
            {"meal":"Lunch","idea":"Bean & veg wrap","notes":"Quick, high fibre"},
            {"meal":"Snack","idea":"Banana + peanut butter","notes":"Budget, <5m"},
            {"meal":"Dinner","idea":"Stir-fry tofu & veg + rice","notes":"<20m, vegan/vegetarian"}
        ]
    return out

def compute_plan(activity, traits, goal, sleep_hours, stress):
    # Base calories by activity
    base = {"Low": 1800, "Moderate": 2200, "High": 2600}.get(activity, 2000)

    # Goal adjustment
    if goal == "loss":
        base -= 250
    elif goal == "gain":
        base += 250

    # Trait biases
    carb_bias = -0.05 if "High Carb Sensitivity" in traits else 0.0
    fat_bias  = -0.05 if "Slow Fat Metabolism" in traits else 0.0
    protein_bias = 0.05 if ("High Carb Sensitivity" in traits or "Slow Fat Metabolism" in traits) else 0.0

    # Sleep & stress nudges
    sh = parse_float(sleep_hours, 7.0)
    if sh < 6:
        protein_bias += 0.05  # protect lean mass with low sleep

    stress_carb_delta = -0.05 if stress == "high" else 0.0

    carbs   = max(0.35 + carb_bias + stress_carb_delta, 0.20)
    fats    = max(0.30 + fat_bias, 0.20)
    protein = max(0.35 + protein_bias, 0.25)

    total = carbs + fats + protein
    carbs, fats, protein = carbs/total, fats/total, protein/total

    macros = {
        "carbs":   round(carbs*100),
        "fats":    round(fats*100),
        "protein": round(protein*100)
    }
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

    # New lifestyle fields
    goal       = (request.form.get("goal") or "").strip()
    diet       = (request.form.get("diet") or "regular").strip()
    allergies  = request.form.getlist("allergies")
    sleep_hrs  = (request.form.get("sleep_hours") or "").strip()
    stress     = (request.form.get("stress") or "").strip()
    water      = (request.form.get("water") or "").strip()
    budget     = (request.form.get("budget") or "").strip()
    cook_time  = (request.form.get("cook_time") or "").strip()
    prefs      = request.form.getlist("prefs")
    traits     = request.form.getlist("traits")  # existing checkboxes

    # Save upload
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_email = email.replace("@","_at_").replace(".","_")
    filename = f"{safe_email}_{ts}.txt"
    path = os.path.join("uploads", filename)
    dna_file.save(path)

    # Minimal DNA summary
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

    # Log
    try:
        log_csv([
            datetime.utcnow().isoformat(), name, email, activity, goal, diet,
            "|".join(allergies), sleep_hrs, stress, water, budget, cook_time,
            "|".join(prefs), "|".join(traits), filename, dna_lines
        ])
    except Exception as e:
        print("CSV log error:", e)

    token = PDF_SIGNER.dumps(payload)

return render_template(
    "result.html",
    token=token,   # <-- this line MUST be present
    name=name, email=email, activity=activity, traits=traits,
    goal=goal, diet=diet, allergies=allergies, sleep_hours=sleep_hrs,
    stress=stress, water=water, budget=budget, cook_time=cook_time,
    calories=calories, macros=macros, plan=plan, dna_summary=dna_summary
)


# JSON endpoint for mobile apps (optional but useful)
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

    try:
        log_csv([
            datetime.utcnow().isoformat(), name, email, activity, goal, diet,
            "|".join(allergies), sleep_hrs, stress, water, budget, cook_time,
            "|".join(prefs), "|".join(traits), filename, dna_lines
        ])
    except Exception as e:
        print("CSV log error:", e)

    return jsonify({
        "ok": True,
        "name": name, "email": email, "activity": activity, "traits": traits,
        "goal": goal, "diet": diet, "allergies": allergies, "sleep_hours": sleep_hrs,
        "stress": stress, "water": water, "budget": budget, "cook_time": cook_time,
        "calories": calories, "macros": macros, "plan": plan, "dna_summary": dna_summary
    }), 200

@app.route("/healthz")
def healthz():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
