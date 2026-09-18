import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
try:
    import serial.tools.list_ports as lp
    print("--- COM ports ---")
    for p in lp.comports():
        print("  %-8s vid=%s pid=%s  %s" % (p.device,
              ("%04X" % p.vid) if p.vid else "-",
              ("%04X" % p.pid) if p.pid else "-", p.description))
except Exception as e:
    print("list_ports failed:", type(e).__name__, e)

print("--- find_board() ---")
try:
    from h723_client import find_board
    print("  ->", find_board())
except Exception as e:
    print("  failed:", type(e).__name__, e)
