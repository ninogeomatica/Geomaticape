import os
import gc
import numpy as np
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBand,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException
)
from qgis import processing
from osgeo import gdal


# =====================================================================
#  CATALOGO DE INDICES ESPECTRALES
# =====================================================================
#  cada indice define:
#    - codigo corto
#    - nombre largo
#    - bandas requeridas (subset de BLUE/GREEN/RED/REDEDGE/NIR/SWIR1/SWIR2)
#    - formula (lambda con un dict de arrays de bandas)
#    - descripcion corta
# =====================================================================

INDICES = {
    "NDVI": {
        "name": "NDVI - Indice de Vegetacion Diferencia Normalizada",
        "bands": ["NIR", "RED"],
        "formula": "(NIR - RED) / (NIR + RED)",
        "func": lambda b: (b["NIR"] - b["RED"]) / (b["NIR"] + b["RED"]),
    },
    "SAVI": {
        "name": "SAVI - Indice de Vegetacion Ajustado al Suelo (L=0.5)",
        "bands": ["NIR", "RED"],
        "formula": "((NIR - RED) / (NIR + RED + 0.5)) * (1 + 0.5)",
        "func": lambda b: ((b["NIR"] - b["RED"]) / (b["NIR"] + b["RED"] + 0.5)) * (1 + 0.5),
    },
    "MSAVI": {
        "name": "MSAVI - SAVI Modificado",
        "bands": ["NIR", "RED"],
        "formula": "0.5 * (2*NIR + 1 - sqrt((2*NIR + 1)^2 - 8*(NIR - RED)))",
        "func": lambda b: 0.5 * (
            2 * b["NIR"] + 1 - np.sqrt(np.maximum(
                (2 * b["NIR"] + 1) ** 2 - 8 * (b["NIR"] - b["RED"]), 0
            ))
        ),
    },
    "EVI": {
        "name": "EVI - Indice de Vegetacion Mejorado",
        "bands": ["NIR", "RED", "BLUE"],
        "formula": "2.5 * (NIR - RED) / (NIR + 6*RED - 7.5*BLUE + 1)",
        "func": lambda b: 2.5 * (b["NIR"] - b["RED"]) / (b["NIR"] + 6 * b["RED"] - 7.5 * b["BLUE"] + 1),
    },
    "EVI2": {
        "name": "EVI2 - EVI sin banda Blue (2 bandas)",
        "bands": ["NIR", "RED"],
        "formula": "2.4 * (NIR - RED) / (NIR + RED + 1)",
        "func": lambda b: 2.4 * (b["NIR"] - b["RED"]) / (b["NIR"] + b["RED"] + 1),
    },
    "GNDVI": {
        "name": "GNDVI - NDVI Verde",
        "bands": ["NIR", "GREEN"],
        "formula": "(NIR - GREEN) / (NIR + GREEN)",
        "func": lambda b: (b["NIR"] - b["GREEN"]) / (b["NIR"] + b["GREEN"]),
    },
    "GCI": {
        "name": "GCI - Indice de Clorofila Verde",
        "bands": ["NIR", "GREEN"],
        "formula": "(NIR / GREEN) - 1",
        "func": lambda b: (b["NIR"] / b["GREEN"]) - 1,
    },
    "NDREI": {
        "name": "NDREI - NDVI Red-Edge (requiere Red Edge)",
        "bands": ["NIR", "REDEDGE"],
        "formula": "(NIR - REDEDGE) / (NIR + REDEDGE)",
        "func": lambda b: (b["NIR"] - b["REDEDGE"]) / (b["NIR"] + b["REDEDGE"]),
    },
    "NDWI": {
        "name": "NDWI (McFeeters) - Agua",
        "bands": ["GREEN", "NIR"],
        "formula": "(GREEN - NIR) / (GREEN + NIR)",
        "func": lambda b: (b["GREEN"] - b["NIR"]) / (b["GREEN"] + b["NIR"]),
    },
    "MNDWI": {
        "name": "MNDWI - NDWI Modificado (Xu)",
        "bands": ["GREEN", "SWIR1"],
        "formula": "(GREEN - SWIR1) / (GREEN + SWIR1)",
        "func": lambda b: (b["GREEN"] - b["SWIR1"]) / (b["GREEN"] + b["SWIR1"]),
    },
    "NDMI": {
        "name": "NDMI / NDII - Humedad de la vegetacion",
        "bands": ["NIR", "SWIR1"],
        "formula": "(NIR - SWIR1) / (NIR + SWIR1)",
        "func": lambda b: (b["NIR"] - b["SWIR1"]) / (b["NIR"] + b["SWIR1"]),
    },
    "NBR": {
        "name": "NBR - Indice de Areas Quemadas",
        "bands": ["NIR", "SWIR2"],
        "formula": "(NIR - SWIR2) / (NIR + SWIR2)",
        "func": lambda b: (b["NIR"] - b["SWIR2"]) / (b["NIR"] + b["SWIR2"]),
    },
    "NBR2": {
        "name": "NBR2 - Severidad de quema secundaria",
        "bands": ["SWIR1", "SWIR2"],
        "formula": "(SWIR1 - SWIR2) / (SWIR1 + SWIR2)",
        "func": lambda b: (b["SWIR1"] - b["SWIR2"]) / (b["SWIR1"] + b["SWIR2"]),
    },
    "NDSI": {
        "name": "NDSI - Indice Diferencia Normalizada de Nieve",
        "bands": ["GREEN", "SWIR1"],
        "formula": "(GREEN - SWIR1) / (GREEN + SWIR1)",
        "func": lambda b: (b["GREEN"] - b["SWIR1"]) / (b["GREEN"] + b["SWIR1"]),
    },
    "BSI": {
        "name": "BSI - Indice de Suelo Desnudo",
        "bands": ["SWIR1", "RED", "NIR", "BLUE"],
        "formula": "((SWIR1 + RED) - (NIR + BLUE)) / ((SWIR1 + RED) + (NIR + BLUE))",
        "func": lambda b: ((b["SWIR1"] + b["RED"]) - (b["NIR"] + b["BLUE"]))
                          / ((b["SWIR1"] + b["RED"]) + (b["NIR"] + b["BLUE"])),
    },
    "VARI": {
        "name": "VARI - Visible Atmospherically Resistant Index (RGB)",
        "bands": ["GREEN", "RED", "BLUE"],
        "formula": "(GREEN - RED) / (GREEN + RED - BLUE)",
        "func": lambda b: (b["GREEN"] - b["RED"]) / (b["GREEN"] + b["RED"] - b["BLUE"]),
    },
    "NGRDI": {
        "name": "NGRDI - Diferencia Normalizada Verde-Rojo (RGB)",
        "bands": ["GREEN", "RED"],
        "formula": "(GREEN - RED) / (GREEN + RED)",
        "func": lambda b: (b["GREEN"] - b["RED"]) / (b["GREEN"] + b["RED"]),
    },
}

INDEX_KEYS = list(INDICES.keys())

# Roles de banda gestionados por el algoritmo
BAND_ROLES = ["BLUE", "GREEN", "RED", "REDEDGE", "NIR", "SWIR1", "SWIR2"]


class IndicesEspectrales(QgsProcessingAlgorithm):
    """
    Calcula indices espectrales (NDVI, SAVI, EVI, NDWI, NBR, NDSI, etc.)
    a partir de una imagen multiespectral. El usuario elige el indice y
    asigna que numero de banda del raster corresponde a cada rol espectral
    (BLUE, GREEN, RED, REDEDGE, NIR, SWIR1, SWIR2).
    Solo es obligatorio asignar las bandas que el indice elegido requiere.
    """

    INPUT_RASTER = "INPUT_RASTER"
    INDEX_TYPE   = "INDEX_TYPE"
    OUTPUT_RASTER = "OUTPUT_RASTER"

    # Parametros de banda por rol
    BAND_BLUE    = "BAND_BLUE"
    BAND_GREEN   = "BAND_GREEN"
    BAND_RED     = "BAND_RED"
    BAND_REDEDGE = "BAND_REDEDGE"
    BAND_NIR     = "BAND_NIR"
    BAND_SWIR1   = "BAND_SWIR1"
    BAND_SWIR2   = "BAND_SWIR2"

    ROLE_PARAM = {
        "BLUE":    BAND_BLUE,
        "GREEN":   BAND_GREEN,
        "RED":     BAND_RED,
        "REDEDGE": BAND_REDEDGE,
        "NIR":     BAND_NIR,
        "SWIR1":   BAND_SWIR1,
        "SWIR2":   BAND_SWIR2,
    }

    # -------------------------------------------------------
    # IDENTIFICACION
    # -------------------------------------------------------

    def name(self):
        return "indices_espectrales"

    def displayName(self):
        return "Indices espectrales (NDVI, SAVI, EVI, NDWI, NBR, NDSI...)"

    def group(self):
        return "Procesamiento"

    def groupId(self):
        return "geomaticape_procesamiento"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), "..", "Icons", "indices.png"))

    def createInstance(self):
        return IndicesEspectrales()

    # -------------------------------------------------------
    # AYUDA
    # -------------------------------------------------------

    def shortHelpString(self):
        rows = []
        for k, v in INDICES.items():
            rows.append("<li><b>{0}</b>: {1}<br/>"
                        "&nbsp;&nbsp;Bandas: {2}<br/>"
                        "&nbsp;&nbsp;Formula: <code>{3}</code></li>"
                        .format(k, v["name"], ", ".join(v["bands"]), v["formula"]))
        catalogo = "\n".join(rows)

        return f"""
<h3>Indices espectrales multiespectrales</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.5<br><br>

<b>Descripcion:</b><br>
Calcula indices espectrales sobre una imagen multiespectral de cualquier
satelite (Landsat, Sentinel-2, CBERS-04A, PlanetScope, RapidEye, etc.).
El usuario asigna que numero de banda del raster corresponde a cada rol
espectral (BLUE / GREEN / RED / REDEDGE / NIR / SWIR1 / SWIR2). Solo
deben rellenarse las bandas requeridas por el indice seleccionado.

<b>Ejemplo Landsat 8 SR (6 bandas: B2..B7):</b><br>
B1=Blue, B2=Green, B3=Red, B4=NIR, B5=SWIR1, B6=SWIR2

<b>Ejemplo Sentinel-2 L2A (Blue=B2, Green=B3, Red=B4, RedEdge=B5, NIR=B8, SWIR1=B11, SWIR2=B12)</b><br>

<b>Indices disponibles ({len(INDICES)}):</b>
<ul>
{catalogo}
</ul>

<b>Salida:</b> raster GeoTIFF de 1 banda (Float32, LZW), con georreferencia
y CRS conservados de la imagen de entrada.

<b>Web:</b> https://www.geomatica.pe/
"""

    # -------------------------------------------------------
    # PARAMETROS
    # -------------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                "Imagen multiespectral de entrada"
            )
        )

        # Lista de indices disponibles (label legible)
        labels = [f"{k}  -  {INDICES[k]['name']}" for k in INDEX_KEYS]
        self.addParameter(
            QgsProcessingParameterEnum(
                self.INDEX_TYPE,
                "Indice espectral a calcular",
                options=labels,
                defaultValue=0,  # NDVI por defecto
                allowMultiple=False
            )
        )

        # Banda BLUE
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_BLUE,
                "Banda BLUE  (azul) - usada por: EVI, BSI, VARI",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda GREEN
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_GREEN,
                "Banda GREEN (verde) - usada por: NDWI, MNDWI, NDSI, GNDVI, GCI, VARI, NGRDI",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda RED
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_RED,
                "Banda RED (rojo) - usada por: NDVI, SAVI, MSAVI, EVI, EVI2, BSI, VARI, NGRDI",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda RED EDGE
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_REDEDGE,
                "Banda RED EDGE (borde rojo) - usada por: NDREI  (Sentinel-2 B5/B6/B7)",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda NIR
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_NIR,
                "Banda NIR (infrarrojo cercano) - usada por casi todos los indices vegetacion/agua",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda SWIR1
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_SWIR1,
                "Banda SWIR1 - usada por: MNDWI, NDMI, NDSI, BSI, NBR2",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda SWIR2
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_SWIR2,
                "Banda SWIR2 - usada por: NBR, NBR2",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_RASTER,
                "Raster del indice (1 banda Float32)"
            )
        )

    # -------------------------------------------------------
    # PROCESO PRINCIPAL
    # -------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster_layer is None:
            raise QgsProcessingException("No se pudo leer el raster de entrada.")
        input_path = raster_layer.source()

        idx_pos    = self.parameterAsEnum(parameters, self.INDEX_TYPE, context)
        index_key  = INDEX_KEYS[idx_pos]
        index_def  = INDICES[index_key]
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT_RASTER, context)

        feedback.pushInfo("====================================================")
        feedback.pushInfo(f"Indice seleccionado: {index_key}")
        feedback.pushInfo(f"  Nombre  : {index_def['name']}")
        feedback.pushInfo(f"  Formula : {index_def['formula']}")
        feedback.pushInfo(f"  Bandas  : {', '.join(index_def['bands'])}")
        feedback.pushInfo("====================================================")

        # Leer numero de banda asignado a cada rol (None si no se asigno)
        band_assignment = {}
        for role, pname in self.ROLE_PARAM.items():
            try:
                val = self.parameterAsInt(parameters, pname, context)
            except Exception:
                val = 0
            # En QGIS, banda 0 = no asignada para parametros optional
            band_assignment[role] = val if val and val > 0 else None

        # Validar que estan asignadas todas las bandas requeridas por el indice
        faltantes = [r for r in index_def["bands"] if band_assignment.get(r) is None]
        if faltantes:
            raise QgsProcessingException(
                f"El indice {index_key} requiere asignar las bandas: "
                f"{', '.join(faltantes)}. Por favor seleccionalas en los "
                "parametros de banda correspondientes."
            )

        # Abrir el raster con GDAL
        ds_in = gdal.Open(input_path, gdal.GA_ReadOnly)
        if ds_in is None:
            raise QgsProcessingException("GDAL no pudo abrir la imagen.")

        cols   = ds_in.RasterXSize
        rows   = ds_in.RasterYSize
        nbands = ds_in.RasterCount
        gt     = ds_in.GetGeoTransform()
        proj   = ds_in.GetProjection()

        feedback.pushInfo(f"Imagen     : {os.path.basename(input_path)}")
        feedback.pushInfo(f"Dimensiones: {cols} x {rows} px, {nbands} bandas")
        feedback.pushInfo(f"Resolucion : {abs(gt[1]):.2f} x {abs(gt[5]):.2f} m")

        # Verificar que los numeros de banda asignados existen en el raster
        for role in index_def["bands"]:
            n = band_assignment[role]
            if n < 1 or n > nbands:
                raise QgsProcessingException(
                    f"La banda asignada al rol {role} (#{n}) no existe en la imagen "
                    f"(la imagen tiene {nbands} banda(s))."
                )

        feedback.setProgress(10)

        # ---------------------------------------------------
        # LEER SOLO LAS BANDAS NECESARIAS
        # ---------------------------------------------------
        feedback.pushInfo("Leyendo bandas requeridas...")
        bandas = {}
        for role in index_def["bands"]:
            n = band_assignment[role]
            band = ds_in.GetRasterBand(n)
            arr = band.ReadAsArray().astype(np.float64)
            nd = band.GetNoDataValue()
            if nd is not None:
                arr[arr == nd] = np.nan
            bandas[role] = arr
            feedback.pushInfo(f"  {role:<8} <- banda #{n} ({band.GetDescription() or 'sin nombre'})")

        ds_in = None
        feedback.setProgress(40)

        # ---------------------------------------------------
        # APLICAR LA FORMULA
        # ---------------------------------------------------
        feedback.pushInfo(f"Calculando {index_key}...")
        with np.errstate(divide="ignore", invalid="ignore"):
            result = index_def["func"](bandas)

        # Reemplazar inf y -inf por NaN (divisiones invalidas)
        result = np.where(np.isfinite(result), result, np.nan)

        # Estadisticas en el log
        valid = result[np.isfinite(result)]
        if valid.size > 0:
            feedback.pushInfo(f"  min  = {valid.min():.4f}")
            feedback.pushInfo(f"  max  = {valid.max():.4f}")
            feedback.pushInfo(f"  mean = {valid.mean():.4f}")
            feedback.pushInfo(f"  std  = {valid.std():.4f}")
            feedback.pushInfo(f"  pixeles validos: {valid.size:,} de {result.size:,}")

        feedback.setProgress(70)

        # ---------------------------------------------------
        # EXPORTAR
        # ---------------------------------------------------
        feedback.pushInfo(f"Exportando resultado a: {output_path}")

        nodata_out = -9999.0
        out_arr = np.where(np.isnan(result), nodata_out, result).astype(np.float32)

        driver = gdal.GetDriverByName("GTiff")
        ds_out = driver.Create(
            output_path, cols, rows, 1, gdal.GDT_Float32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out.SetGeoTransform(gt)
        ds_out.SetProjection(proj)

        out_band = ds_out.GetRasterBand(1)
        out_band.WriteArray(out_arr)
        out_band.SetDescription(index_key)
        out_band.SetNoDataValue(nodata_out)

        ds_out.FlushCache()
        ds_out = None

        feedback.setProgress(100)

        # Limpieza
        del bandas, result, out_arr
        gc.collect()

        feedback.pushInfo(f"INDICE {index_key} COMPLETO")

        return {self.OUTPUT_RASTER: output_path}

    def run(self):
        processing.execAlgorithmDialog(self)
