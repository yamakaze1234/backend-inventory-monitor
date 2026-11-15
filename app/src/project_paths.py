"""集中定义源码、资源和数据入口；打包版继续使用现有目录。"""
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else SOURCE_ROOT.parents[1]
UI_ROOT = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else PROJECT_ROOT / 'app' / 'ui'
DOC_ROOT = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else PROJECT_ROOT / 'docs'

# 独立项目与旧版运行数据分开存放，打包版仍保留原安装布局。
INDEPENDENT_ROOT = PROJECT_ROOT.parent / "独立工具"
LEGACY_DATA_ROOT = PROJECT_ROOT if getattr(sys, "frozen", False) else PROJECT_ROOT / "app" / "data" / "legacy-root"
