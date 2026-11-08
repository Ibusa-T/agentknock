import sys
from pathlib import Path

# console_injector.py を import 可能にするため tools ディレクトリを sys.path に追加
_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
