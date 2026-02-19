from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
import os

from .Script.factor_landsat import FactorLandsat
from .Script.factor_sentinel2_l1a import FactorSentinel2L1A
from .Script.factor_sentinel2_l2a import FactorSentinel2L2A

class GeomaticapePlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = "Geomaticape"
        self.actions = []

    def initGui(self):

        self.add_action("Factor escala Landsat",
                        "Icons/landsat.png",
                        FactorLandsat)

        self.add_action("Factor escala Sentinel2 L1A",
                        "Icons/sentinel2l1a.png",
                        FactorSentinel2L1A)
        
        self.add_action("Factor escala Sentinel2 L2A",
                        "Icons/sentinel2l2a.png",
                        FactorSentinel2L2A)

    def add_action(self, text, icon_path, tool_class):

        icon = QIcon(os.path.join(self.plugin_dir, icon_path))
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(lambda: tool_class().run())

        self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
