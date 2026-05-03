from qgis.PyQt.QtWidgets import QAction, QMenu
from qgis.PyQt.QtGui import QIcon
import os

# Conversion (factores de escala / DN -> Reflectancia)
from .Script.firma_espectral import FirmaEspectral
from .Script.rs_landsat_c2_l1 import RSLandSatC2L1
from .Script.factor_landsat import FactorLandsat
from .Script.factor_sentinel2_l1a import FactorSentinel2L1A
from .Script.factor_sentinel2_l2a import FactorSentinel2L2A
from .Script.factor_modis09 import FactorMODIS09
from .Script.factor_modis11 import FactorMODIS11
from .Script.factor_modis12 import FactorMODIS12
from .Script.factor_modis13 import FactorMODIS13

# Procesamiento (analisis avanzado + clasificacion + extraccion bandas)
from .Script.cbers04a_pansharp import CBERS04APansharp
from .Script.landsat_pansharpening import LandsatPansharpening
from .Script.acp_satelite import ACPSatelite
from .Script.indices_espectrales import IndicesEspectrales
from .Script.clasificacion_no_supervisada import ClasificacionNoSupervisada
from .Script.clasificacion_supervisada import ClasificacionSupervisada
from .Script.extraer_bandas_multiespectral import ExtraerBandasMultiespectral
from .Script.combinar_bandas_nombres import CombinarBandasNombres
from .Script.recortar_rasters_zona import RecortarRastersZona
from .Script.tasseled_cap import TasseledCap
from .Script.clasificar_raster import ClasificarRaster
from .Script.reclasificar_raster import ReclasificarRaster
from .Script.reporte_clasificacion import ReporteClasificacion
from .Script.raster_mosaico_imagenes import RasterMosaicoImagenes
from .Script.raster_definir_celdas_nulas import RasterDefinirCeldasNulas

# Geoprocesamiento (vector / tabular / zonal / sampling)
from .Script.crear_poligonos_tabla import CrearPoligonosTabla
from .Script.estadistica_zonal_raster import EstadisticaZonalRaster
from .Script.extraer_valores_puntuales import ExtraerValoresPuntuales

# Vector — nuevas herramientas
from .Script.vector_angulo_poligono import VectorAnguloPoligono
from .Script.vector_poligono_superpuesto import VectorPoligonoSuperpuesto
from .Script.vector_sucesion_cruzada import VectorSucesionCruzada

# MDE — nuevas herramientas
from .Script.mde_descargar_mde import MDEDescargarMDE
from .Script.mde_punto_cota_dem import MDEPuntoCotaDEM
from .Script.mde_curvas_nivel_intermedias import MDECurvasNivelIntermedias


class GeomaticapePlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = None
        self.menu_conv = None
        self.menu_proc = None
        self.menu_geo  = None
        self.menu_post = None
        self.menu_vec  = None
        self.menu_mde  = None

    def initGui(self):

        logo_path = os.path.join(self.plugin_dir, "Icons", "logo_geomatica.png")
        self.menu = QMenu("Geomaticape", self.iface.mainWindow())
        self.menu.setIcon(QIcon(logo_path))
        self.iface.pluginMenu().addMenu(self.menu)

        # ── CONVERSION ──────────────────────────────────────────
        self.menu_conv = QMenu("Conversion", self.menu)
        self.menu_conv.setIcon(QIcon(os.path.join(self.plugin_dir, "Icons", "landsat.png")))
        self.menu.addMenu(self.menu_conv)

        self.menu_conv.addSeparator()

        # Landsat / Sentinel
        self.add_action(self.menu_conv, "RS LandSat C2 L1 (SR + LST + PAN)",
                        "Icons/landsat.png", RSLandSatC2L1)
        self.add_action(self.menu_conv, "Factor escala Landsat C2 L2",
                        "Icons/landsat.png", FactorLandsat)
        self.add_action(self.menu_conv, "Factor escala Sentinel2 L1A",
                        "Icons/sentinel2l1a.png", FactorSentinel2L1A)
        self.add_action(self.menu_conv, "Factor escala Sentinel2 L2A",
                        "Icons/sentinel2l2a.png", FactorSentinel2L2A)

        self.menu_conv.addSeparator()

        # MODIS — 4 herramientas independientes
        self.add_action(self.menu_conv, "Factor escala MODIS 09 (Reflectancia Superficial)",
                        "Icons/indices.png", FactorMODIS09)
        self.add_action(self.menu_conv, "Factor escala MODIS 11 (LST °C)",
                        "Icons/indices.png", FactorMODIS11)
        self.add_action(self.menu_conv, "Factor escala MODIS 12 (Cobertura del Suelo)",
                        "Icons/clasificacion.png", FactorMODIS12)
        self.add_action(self.menu_conv, "Factor escala MODIS 13 (NDVI / EVI)",
                        "Icons/indices.png", FactorMODIS13)

        self.menu.addSeparator()

        # ── PROCESAMIENTO ────────────────────────────────────────
        self.menu_proc = QMenu("Procesamiento", self.menu)
        self.menu_proc.setIcon(QIcon(os.path.join(self.plugin_dir, "Icons", "acp.png")))
        self.menu.addMenu(self.menu_proc)

        self.add_action(self.menu_proc, "CBERS-04A Pansharpening 2m",
                        "Icons/CBERS04A.png", CBERS04APansharp)
        self.add_action(self.menu_proc, "Landsat Pansharpening 30m -> 15m (Weighted Brovey)",
                        "Icons/landsat.png", LandsatPansharpening)
        self.add_action(self.menu_proc, "ACP Multiespectral (cualquier satelite)",
                        "Icons/acp.png", ACPSatelite)
        self.add_action(self.menu_proc, "Tasseled Cap (Brightness · Greenness · Wetness)",
                        "Icons/indices.png", TasseledCap)
        self.add_action(self.menu_proc, "Indices espectrales (NDVI, SAVI, EVI, NDWI...)",
                        "Icons/indices.png", IndicesEspectrales)
        self.add_action(self.menu_proc, "Extraer bandas de imagenes multiespectrales",
                        "Icons/extraer_bandas.png", ExtraerBandasMultiespectral)
        self.add_action(self.menu_proc, "Combinar bandas con nombres (Red, NIR, SWIR1...)",
                        "Icons/combinar_bandas.png", CombinarBandasNombres)
        self.add_action(self.menu_proc, "Recortar raster por zona de estudio (cutline / bbox)",
                        "Icons/poligonos_tabla.png", RecortarRastersZona)
        self.add_action(self.menu_proc, "Firma espectral (Landsat 5/7/8/9 · Sentinel-2 · ASTER)",
                        "Icons/indices.png", FirmaEspectral)

        self.menu_proc.addSeparator()

        self.add_action(self.menu_proc, "Clasificacion no supervisada (K-Means, GMM, ISODATA, Birch)",
                        "Icons/clasificacion.png", ClasificacionNoSupervisada)
        self.add_action(self.menu_proc, "Clasificacion supervisada y validacion",
                        "Icons/clasif_supervisada.png", ClasificacionSupervisada)
        self.add_action(self.menu_proc, "Mosaico de imagenes",
                        "Icons/indices.png", RasterMosaicoImagenes)
        self.add_action(self.menu_proc, "Definir celdas nulas",
                        "Icons/indices.png", RasterDefinirCeldasNulas)


        self.menu.addSeparator()

        # ── POSTPROCESAMIENTO ────────────────────────────────────
        self.menu_post = QMenu("PostProcesamiento", self.menu)
        self.menu_post.setIcon(QIcon(os.path.join(self.plugin_dir, "Icons", "clasificacion.png")))
        self.menu.addMenu(self.menu_post)

        self.add_action(self.menu_post, "Clasificar raster por rangos (min / max / valor)",
                        "Icons/clasificacion.png", ClasificarRaster)
        self.add_action(self.menu_post, "Reclasificar raster (remapeo de valores)",
                        "Icons/clasificacion.png", ReclasificarRaster)
        self.add_action(self.menu_post, "Reporte de clasificacion (area · porcentaje · estadisticas)",
                        "Icons/zonal_raster.png", ReporteClasificacion)
        
        self.menu.addSeparator()

        # ── GEOPROCESAMIENTO ─────────────────────────────────────
        self.menu_geo = QMenu("Geoprocesamiento", self.menu)
        self.menu_geo.setIcon(QIcon(os.path.join(self.plugin_dir, "Icons", "poligonos_tabla.png")))
        self.menu.addMenu(self.menu_geo)

        self.add_action(self.menu_geo, "Crear poligonos a partir de tabla (CSV/XLSX/TXT)",
                        "Icons/poligonos_tabla.png", CrearPoligonosTabla)
        self.add_action(self.menu_geo, "Estadistica zonal raster (Excel/CSV)",
                        "Icons/zonal_raster.png", EstadisticaZonalRaster)
        self.add_action(self.menu_geo, "Extraer valores puntuales de multiples raster",
                        "Icons/extraer_valores.png", ExtraerValoresPuntuales)

        self.menu.addSeparator()

        # ── VECTOR ───────────────────────────────────────────────
        self.menu_vec = QMenu("Vector", self.menu)
        self.menu_vec.setIcon(QIcon(os.path.join(self.plugin_dir, "Icons", "poligonos_tabla.png")))
        self.menu.addMenu(self.menu_vec)

        self.add_action(self.menu_vec, "Calcular angulo de poligono",
                        "Icons/poligonos_tabla.png", VectorAnguloPoligono)
        self.add_action(self.menu_vec, "Poligono superpuesto propio",
                        "Icons/poligonos_tabla.png", VectorPoligonoSuperpuesto)
        self.add_action(self.menu_vec, "Secciones transversales",
                        "Icons/poligonos_tabla.png", VectorSucesionCruzada)

        self.menu.addSeparator()

        # ── MDE ──────────────────────────────────────────────────
        self.menu_mde = QMenu("MDE", self.menu)
        self.menu_mde.setIcon(QIcon(os.path.join(self.plugin_dir, "Icons", "extraer_valores.png")))
        self.menu.addMenu(self.menu_mde)

        self.add_action(self.menu_mde, "Descargar MDE",
                        "Icons/extraer_valores.png", MDEDescargarMDE)
        #self.add_action(self.menu_mde, "Establecer coordenada Z desde MDE",
        #                "Icons/extraer_valores.png", MDEEstablecerZdesdeMDE)
        self.add_action(self.menu_mde, "Generar elevaciones puntuales",
                        "Icons/extraer_valores.png", MDEPuntoCotaDEM)
        self.add_action(self.menu_mde, "Extraer curvas de nivel intermedias",
                        "Icons/extraer_valores.png", MDECurvasNivelIntermedias)

    def add_action(self, parent_menu, text, icon_path, tool_class):
        icon   = QIcon(os.path.join(self.plugin_dir, icon_path))
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(lambda: tool_class().run())
        parent_menu.addAction(action)
        self.actions.append(action)

    def unload(self):
        if self.menu:
            self.iface.pluginMenu().removeAction(self.menu.menuAction())
            self.menu = None
            self.menu_conv = None
            self.menu_proc = None
            self.menu_geo  = None
            self.menu_post = None
            self.menu_vec  = None
            self.menu_mde  = None
