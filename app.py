from flask import Flask, request, jsonify, abort
import requests
from flask import render_template
import os, json
import AIAgent

app = Flask(__name__)

# Ensure templates folder exists
template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
if not os.path.exists(template_dir):
    os.makedirs(template_dir)
app.template_folder = template_dir

@app.errorhandler(400)
def bad_request(error):
    """
    Custom error handler for 400 Bad Request.
    """
    return jsonify({"error": "Bad Request", "message": error.description}), 400

@app.route("/", methods=["GET"])
def index():
    """
    Simple index route to confirm the API is running.
    """
    return render_template("index.html")

@app.route("/dashboard", methods=["GET"])
def dashboard():
    """
    Render the dashboard HTML page.
    """
    return render_template("trial_insights_dashboard.html")

@app.route("/studies", methods=["GET"])
def get_studies():
    """
    Fetch studies from ClinicalTrials.gov based on condition and overall status.

    Query Parameters:
    - condition (required): Medical condition to search for (e.g., "plaque psoriasis").
    - status (optional): Overall study status filter (default: "COMPLETED").
    - limit (optional): Number of results to return (default: 10).
    - offset (optional): Pagination offset (default: 0).
    """
    condition = request.args.get("condition")
    status = request.args.get("status", "COMPLETED")
    try:
        limit = int(request.args.get("limit", 10))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        abort(400, description="`limit` and `offset` must be integers")

    if not condition:
        abort(400, description="Missing required query parameter: condition")

    base_url = "https://clinicaltrials.gov/api/v2/studies"
    params = {
        "format": "json",
        "query.cond": condition,
        "filter.overallStatus": status,
        "pageSize": limit,
        "countTotal": "true",
        #"page": offset // limit + 1  # ClinicalTrials API uses 1-based page indexing
    }

    try:
        response = requests.get(base_url, params=params, timeout=10.0)
    except requests.RequestException as e:
        abort(502, description=f"Error connecting to ClinicalTrials.gov: {e}")

    if response.status_code != 200:
        abort(response.status_code, description="Failed to fetch data from ClinicalTrials.gov")

    data = response.json()
    studies_list = []
    for study in data.get("studies", []):
        study_data = {
            "resultsSection": study.get("resultsSection", {}),
            "studyId": study.get("protocolSection").get("identificationModule").get("nctId"),
        }
        studies_list.append(study_data)
    ai = AIAgent.AIAgent("gpt-4.1")
    studies_list = ai.read_json_data(json.dumps(studies_list))
    ai_response = ai.query_agent("Summarize the main findings for a patient", studies_list, condition)

    with open("Dr2ndOp/logs/studies.json", "w") as f:
        json.dump(studies_list, f, indent=2)
    import markdown
    return jsonify(markdown.markdown(ai_response))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
