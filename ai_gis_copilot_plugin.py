from pathlib import Path
import urllib.request

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import QAction


class AIGISCopilotPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None

    def initGui(self):
        self._ensure_default_icon_file()
        icon = self._resolve_plugin_icon()
        self._remove_stale_actions()

        self.action = QAction(icon, "AI GIS Copilot", self.iface.mainWindow())
        self.action.setObjectName("AIGISCopilot_MainAction")
        self.action.triggered.connect(self.open_dock)
        self.iface.addPluginToMenu("&AI GIS Copilot", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu("&AI GIS Copilot", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

        if self.dock is not None:
            self.dock.cleanup_before_unload()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def open_dock(self):
        if self.dock is None:
            from .ai_gis_copilot_dock import AIGISCopilotDockWidget

            self.dock = AIGISCopilotDockWidget(self.iface)
            self.iface.addDockWidget(self.dock.default_area, self.dock)
        # Enforce compact dock width on open for visible layout consistency.
        try:
            # resizeDocks expects orientation (Horizontal/Vertical), not dock area.
            self.iface.mainWindow().resizeDocks([self.dock], [420], Qt.Horizontal)
        except Exception:
            pass
        self.dock.show()
        self.dock.raise_()

    def _resolve_plugin_icon(self):
        # Use local file first so toolbar/menu and plugin manager stay consistent.
        for local_name in ("icon.png", "icon.svg"):
            icon_path = Path(__file__).with_name(local_name)
            if icon_path.exists():
                return QIcon(str(icon_path))

        # Last-resort runtime URL load.
        icon_url = "https://cdn-icons-png.flaticon.com/128/820/820875.png"
        try:
            with urllib.request.urlopen(icon_url, timeout=15) as response:
                raw = response.read()
            pixmap = QPixmap()
            if pixmap.loadFromData(raw):
                return QIcon(pixmap)
        except Exception:
            pass
        return QIcon()

    def _ensure_default_icon_file(self):
        icon_path = Path(__file__).with_name("icon.png")
        if icon_path.exists():
            return
        icon_url = "https://cdn-icons-png.flaticon.com/128/820/820875.png"
        try:
            with urllib.request.urlopen(icon_url, timeout=20) as response:
                raw = response.read()
            if raw:
                icon_path.write_bytes(raw)
        except Exception:
            pass

    def _remove_stale_actions(self):
        """
        Defensive cleanup when plugin is reloaded without a clean unload.
        Prevents duplicate toolbar/menu actions.
        """
        mw = self.iface.mainWindow()
        for action_name in ("AIGISCopilot_MainAction",):
            try:
                for act in mw.findChildren(QAction, action_name):
                    try:
                        self.iface.removePluginMenu("&AI GIS Copilot", act)
                    except Exception:
                        pass
                    try:
                        self.iface.removeToolBarIcon(act)
                    except Exception:
                        pass
            except Exception:
                continue
