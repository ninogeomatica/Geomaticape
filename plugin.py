from qgis.PyQt.QtWidgets import QAction, QMenu
from qgis.PyQt.QtGui import QIcon
import os

from .Script.factor_landsat import FactorLandsat
from .Script.factor_sentinel2_l1a import FactorSentinel2L1A
from .Script.factor_sentinel2_l2a import FactorSentinel2L2A
from .Script.cbers04a_pansharp import CBERS04APansharp


class GeomaticapePlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = None  # Se crea en initGui como QMenu con ícono

    def initGui(self):

        # ── Crear el submenú "Geomaticape" con el logo de empresa ──
        logo_path = os.path.join(self.plugin_dir, "Icons", "logo_geomatica.png")
        self.menu = QMenu("Geomaticape", self.iface.mainWindow())
        self.menu.setIcon(QIcon(logo_path))

        # Agregar el submenú al menú "Complementos" de QGIS
        self.iface.pluginMenu().addMenu(self.menu)

        # ── Agregar herramientas al submenú ──
        self.add_action("Factor escala Landsat",
                        "Icons/landsat.png",
                        FactorLandsat)

        self.add_action("Factor escala Sentinel2 L1A",
                        "Icons/sentinel2l1a.png",
                        FactorSentinel2L1A)

        self.add_action("Factor escala Sentinel2 L2A",
                        "Icons/sentinel2l2a.png",
                        FactorSentinel2L2A)

        self.add_action("CBERS-04A Pansharpening 2m",
                        "Icons/cbers04a.png",
                        CBERS04APansharp)

    def add_action(self, text, icon_path, tool_class):

        icon   = QIcon(os.path.join(self.plugin_dir, icon_path))
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(lambda: tool_class().run())

        # Agregar al QMenu con logo (no a iface.addPluginToMenu)
        self.menu.addAction(action)
        self.actions.append(action)

    def unload(self):
        # Eliminar el submenú completo al desactivar el plugin
        if self.menu:
            self.iface.pluginMenu().removeAction(self.menu.menuAction())
            self.menu = None