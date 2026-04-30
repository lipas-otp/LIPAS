# tests/conftest.py —— pytest autoload it, to add project root to sys.path
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
