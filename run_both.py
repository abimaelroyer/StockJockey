import os
import sys
import threading
import robinhood
from config import load_config
from strategy import run_strategy
from api import app

# Console output includes emoji (robinhood.py login messages); don't let a
# non-UTF-8 Windows console turn that into a crash.
sys.stdout.reconfigure(errors="replace")
sys.stderr.reconfigure(errors="replace")

cfg = load_config()
print(f"Config: {cfg['symbol']} {cfg['short_window']}/{cfg['long_window']} SMA crossover, "
      f"{cfg['trade_quantity']} share(s) per trade, {cfg['mode'].upper()} mode")

# Authenticate once, before anything else starts, so the loop thread
# and the Flask process never race each other for the same session.
if robinhood.has_credentials():
    login_result = robinhood._ensure_login()
    print(f"Startup login: {login_result}")
elif cfg["mode"] == "live":
    sys.exit("Live mode needs ROBINHOOD_USER and ROBINHOOD_PASS in .env (see .env.example).")
else:
    print("No Robinhood credentials in .env; paper trading only.")

threading.Thread(target=run_strategy, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)
