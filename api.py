import os
from dotenv import load_dotenv
load_dotenv()
import json
import csv
from flask import Flask, jsonify, request
import datetime
from datetime import datetime, timezone
from flask_cors import CORS

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LEDGER_PATH = os.path.join(SCRIPT_DIR, "trade_ledger.csv")
PARAMS_PATH = os.path.join(SCRIPT_DIR, "strategy_params.json")

app = Flask(__name__)
CORS(app)
API_KEY = os.environ.get("PANEL_API_KEY")


@app.before_request
def check_auth():
    if request.method == "OPTIONS":
        return
    if request.path == "/health":
        return
    if not API_KEY or request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

@app.route("/health")
def health():
    heartbeat_path = os.path.join(STATE_DIR, "heartbeat.json")
    try:
        with open(heartbeat_path) as f:
            beat = json.load(f)
        last = datetime.fromisoformat(beat["time"])
        age_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60
        return jsonify({
            "status": "stale" if age_minutes > 15 else beat.get("status", "ok"),
            "last_cycle": beat["time"],
            "minutes_since": round(age_minutes, 1),
            "detail": beat.get("detail"),
        })
    except FileNotFoundError:
        return jsonify({"status": "no_heartbeat"}), 503
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/positions")
def positions():
    with open(os.path.join(STATE_DIR, "positions.json")) as f:
        return jsonify(json.load(f))


@app.route("/balance")
def balance():
    with open(os.path.join(STATE_DIR, "paper_balance.json")) as f:
        return jsonify(json.load(f))


@app.route("/trades")
def trades():
    with open(LEDGER_PATH, newline="") as f:
        return jsonify(list(csv.DictReader(f)))


@app.route("/params", methods=["GET", "POST"])
def params():
    if request.method == "POST":
        incoming = request.get_json()
        with open(PARAMS_PATH) as f:
            current = json.load(f)
        for key in ("rsi_oversold_threshold", "rsi_overbought_threshold", "universe"):
            if key in incoming:
                current[key] = incoming[key]
        with open(PARAMS_PATH, "w") as f:
            json.dump(current, f, indent=2)
        return jsonify(current)

    with open(PARAMS_PATH) as f:
        return jsonify(json.load(f))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
