import os
import time

# Sample snapshots name result files by local hour at the frozen epoch (05AM in IST)
os.environ["TZ"] = "Asia/Kolkata"
time.tzset()
