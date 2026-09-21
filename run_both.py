import os
import threading
import robinhood
from strategy import run_strategy
from api import app

# Authenticate once, before anything else starts, so the loop thread
# and the Flask process never race each other for the same session.
login_result = robinhood._ensure_login()
print(f"Startup login: {login_result}")

threading.Thread(target=run_strategy, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)