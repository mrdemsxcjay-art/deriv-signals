"""
Suite globale — exécute tous les scripts/test_*.py et résume.
Usage : python scripts/run_all_tests.py
(Étapes suivantes : test_synth.py, test_signals.py, etc. s'ajouteront ici automatiquement.)
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    tests = sorted(glob.glob(os.path.join(ROOT, "scripts", "test_*.py")))
    print(f"🧪 {len(tests)} suite(s) détectée(s)\n")
    results = []
    for t in tests:
        name = os.path.basename(t)
        print(f"{'━' * 64}\n▶ {name}\n{'━' * 64}")
        r = subprocess.run([sys.executable, t], cwd=ROOT)
        results.append((name, r.returncode == 0))
        print()
    print("=" * 64)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    n_ok = sum(1 for _, ok in results if ok)
    print(f"\nTOTAL : {n_ok}/{len(results)} suites vertes")
    print("=" * 64)
    sys.exit(0 if n_ok == len(results) else 1)


if __name__ == "__main__":
    main()
