import os
import threading
from strategy import run_strategy
from api import app

threading.Thread(target=run_strategy, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))