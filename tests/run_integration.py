"""Runs main.py end-to-end against the simulated meter, then sends SIGINT to check the
graceful logoff path. Run directly: python3 tests/run_integration.py"""

import os
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_serial

METER = fake_serial.FakeMeter(byte_order="little")
fake_serial.install(METER)

CONFIG = os.path.join(HERE, "integration.properties")
with open(CONFIG, "w") as f:
    f.write("port=/dev/fake\nbaud=9600\npassword=" + "K" * 20 + "\n"
            "username=OpenHAB\nuserId=1\n"
            "refreshIntervalSeconds=1\nlogoffIntervalSeconds=3\n"
            "idleStartTime=02:10:00\nidleSeconds=480\nlogLevel=DEBUG\n")

sys.argv = ["main.py", CONFIG]
import main as main_mod


def interrupt_later():
    time.sleep(6)
    os.kill(os.getpid(), signal.SIGINT)


threading.Thread(target=interrupt_later, daemon=True).start()
main_mod.main()

os.remove(CONFIG)
reqs = METER.requests
print("\n--- request sequence seen by the meter ---")
print(" ".join("%02X" % r for r in reqs))
assert reqs[:5] == [0x20, 0x61, 0x50, 0x51, 0x30], "session handshake wrong"
assert reqs.count(0x3F) >= 2, "no table reads happened"
assert reqs.count(0x52) >= 1, "no LOGOFF sent"
assert reqs.count(0x21) >= 1, "no TERMINATE sent"
assert reqs[-2:] == [0x52, 0x21], "did not end with logoff+terminate"
assert reqs.count(0x51) >= 2, "session was not recycled after logoffIntervalSeconds"
print("\nIntegration run OK")
