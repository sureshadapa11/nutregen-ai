from flask import Flask, render_template, request, send_file
import os
import json
import csv
from xhtml2pdf import pisa

app = Flask(__name__)

# Load DNA traits reference from JSON
with open("dna_traits.json") as f:
    TRAIT_MAP = json.load(f)

def parse_dna_file(filepath):
    traits_detected = []
    with open(filepath, 'r') as file:
        for line in file:
            if line.startswith("rs"):
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    snp, genotype = parts[0], parts[1]
                    if snp in TRAIT_MAP:
                        gene_info = TRAIT_MAP[snp]
                        trait = gene_info["trait"]
                        genotype_effect = gene_info["genotypes"].get(genotype, None)
                        if genotype_effect:
                            traits_detected.append(f"{trait} ({genotype_effect})")
    return traits_detected

def generate_meal_plan(traits, activity):
    plan = []
    if any("Lactose" in t and "intolerant" in t.lower() for t in traits):
        plan.append("Breakfast: Oats with almond milk and berries")
    else:
        plan.append("Breakfast: Greek yogurt with honey and granola")

    if any("Obesity" in t and "high" in t.lower() for t in traits):
        plan.append("Lunch: Grilled chicken salad with olive oil")
    else:
        plan.append("Lunch: Brown rice with paneer and steamed veggies")

    if any("Caffeine" in t and "slow" in t.lower() for t in traits):
        plan.append("Limit coffee intake after 2 PM")

    plan.append("Snack: Mixed nuts and green tea")
    plan.append("Dinner: Grilled fish or tofu with quinoa")

    if activity == "High":
        plan.append("Post-workout: Protein smoothie with banana")
    elif activity == "Low":
        plan.append("Light evening walk recommended")

    return plan

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate():
    name = request.form["name"]
    email = request.form["email"]
    activity = request.form["activity"]
    dna_file = request.files["dna"]

    filepath = os.path.join("uploads", dna_file.filename)
    os.makedirs("uploads", exist_ok=True)
    dna_file.save(filepath)

    detected_traits = parse_dna_file(filepath)
    meal_plan = generate_meal_plan(detected_traits, activity)

    # Save user record
    with open("user_data.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([name, email, "; ".join(detected_traits)])

    return render_template("result.html", name=name, traits=detected_traits, meal_plan=meal_plan)

@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    name = request.form["name"]
    traits = request.form.getlist("traits")
    meal_plan = request.form.getlist("meal_plan")

    html = render_template("pdf_template.html", name=name, traits=traits, meal_plan=meal_plan)
    pdf_path = f"{name.replace(' ', '_')}_plan.pdf"

    with open(pdf_path, "wb") as pdf_file:
        pisa.CreatePDF(html, dest=pdf_file)

    return send_file(pdf_path, as_attachment=True)

if __name__ == "__main__":
    app.run(debug=True)
