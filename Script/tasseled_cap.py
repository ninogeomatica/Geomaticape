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
#  COEFICIENTES TASSELED CAP POR SENSOR
#  Orden de columnas: BLUE, GREEN, RED, NIR, SWIR1, SWIR2
#  Filas: Brightness, Greenness, Wetness
#
#  Fuentes:
#    Landsat 5  — Crist & Cicone (1984) / Crist (1985)
#    Landsat 7  — Huang et al. (2002)
#    Landsat 8  — Baig et al. (2014)
#    Landsat 9  — Mismos coefs. que L8 (mismo sensor OLI-2, calibración equivalente)
#    Sentinel-2 — Shi & Xu (2019)
# =====================================================================

TC_COEF = {
    "Landsat 5 TM": {
        "Brightness": [ 0.3037,  0.2793,  0.4343,  0.5585,  0.5082,  0.1863],
        "Greenness":  [-0.2848, -0.2435, -0.5436,  0.7243,  0.0840, -0.1800],
        "Wetness":    [ 0.1509,  0.1793,  0.3299,  0.3406, -0.7112, -0.4572],
    },
    "Landsat 7 ETM+": {
        "Brightness": [ 0.3561,  0.3972,  0.3904,  0.6966,  0.2286,  0.1596],
        "Greenness":  [-0.3344, -0.3544, -0.4556,  0.6966, -0.0242, -0.2630],
        "Wetness":    [ 0.2626,  0.2141,  0.0926,  0.0656, -0.7629, -0.5388],
    },
    "Landsat 8 OLI": {
        "Brightness": [ 0.3029,  0.2786,  0.4733,  0.5599,  0.5080,  0.1872],
        "Greenness":  [-0.2941, -0.2430, -0.5424,  0.7276,  0.0713, -0.1608],
        "Wetness":    [ 0.1511,  0.1973,  0.3283,  0.3407, -0.7117, -0.4559],
    },
    "Landsat 9 OLI-2": {
        # OLI-2 tiene respuesta espectral prácticamente idéntica a OLI
        "Brightness": [ 0.3029,  0.2786,  0.4733,  0.5599,  0.5080,  0.1872],
        "Greenness":  [-0.2941, -0.2430, -0.5424,  0.7276,  0.0713, -0.1608],
        "Wetness":    [ 0.1511,  0.1973,  0.3283,  0.3407, -0.7117, -0.4559],
    },
    "Sentinel-2 MSI": {
        "Brightness": [ 0.3510,  0.3813,  0.3437,  0.7196,  0.2396,  0.1949],
        "Greenness":  [-0.3599, -0.3533, -0.4734,  0.6633,  0.0087, -0.2856],
        "Wetness":    [ 0.2578,  0.2305,  0.0883,  0.1071, -0.7611, -0.5308],
    },
}

SENSOR_KEYS = list(TC_COEF.keys())


class TasseledCap(QgsProcessingAlgorithm):
    """
    Transformación Tasseled Cap para imágenes multiespectrales de
    Landsat 5, 7, 8, 9 y Sentinel-2.

    Entrada:
        - Imagen multiespectral con bandas: BLUE, GREEN, RED, NIR, SWIR1, SWIR2
        - Selección del sensor para aplicar los coeficientes correctos
        - Asignación manual de qué número de banda corresponde a cada rol

    Salida:
        - Raster GeoTIFF Float32 de 3 bandas:
            Banda 1: Brightness (Brillo)
            Banda 2: Greenness  (Verdor)
            Banda 3: Wetness    (Humedad)
    """

    INPUT_RASTER  = "INPUT_RASTER"
    SENSOR_TYPE   = "SENSOR_TYPE"
    BAND_BLUE     = "BAND_BLUE"
    BAND_GREEN    = "BAND_GREEN"
    BAND_RED      = "BAND_RED"
    BAND_NIR      = "BAND_NIR"
    BAND_SWIR1    = "BAND_SWIR1"
    BAND_SWIR2    = "BAND_SWIR2"
    OUTPUT_RASTER = "OUTPUT_RASTER"

    # -------------------------------------------------------
    # IDENTIFICACIÓN
    # -------------------------------------------------------

    def name(self):
        return "tasseled_cap"

    def displayName(self):
        return "Tasseled Cap (Brightness · Greenness · Wetness)"

    def group(self):
        return "Procesamiento"

    def groupId(self):
        return "geomaticape_procesamiento"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), "..", "Icons", "indices.png"))

    def createInstance(self):
        return TasseledCap()

    # -------------------------------------------------------
    # AYUDA
    # -------------------------------------------------------

    def shortHelpString(self):
        coef_html = ""
        for sensor, coefs in TC_COEF.items():
            coef_html += f"<b>{sensor}</b><br>"
            for comp, vals in coefs.items():
                b, g, r, n, s1, s2 = vals
                coef_html += (
                    f"&nbsp;&nbsp;{comp} = "
                    f"{b:+.4f}·Blue {g:+.4f}·Green {r:+.4f}·Red "
                    f"{n:+.4f}·NIR {s1:+.4f}·SWIR1 {s2:+.4f}·SWIR2<br>"
                )
            coef_html += "<br>"

        return f"""
<h3>Transformación Tasseled Cap</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Versión:</b> 1.0<br><br>

<b>Descripción:</b><br>
Aplica la transformación Tasseled Cap a una imagen multiespectral, produciendo
tres componentes con significado físico-biológico:<br>
<ul>
<li><b>Brightness (Brillo)</b>: reflectividad global. Sensible a suelo desnudo y urbanización.</li>
<li><b>Greenness (Verdor)</b>: contraste vegetación viva vs suelo/urbano.</li>
<li><b>Wetness (Humedad)</b>: contenido de humedad del suelo y la vegetación.</li>
</ul>

<b>Sensores compatibles y coeficientes aplicados:</b><br>
{coef_html}

<b>Requisito:</b><br>
La imagen debe estar en <b>reflectancia superficial (valores 0–1 o 0–10000)</b>.
Asigne cada número de banda del raster al rol espectral correspondiente.<br><br>

<b>Asignación típica de bandas:</b><br>
<ul>
<li>Landsat 5/7 (SR, 6 bandas): B1=Blue · B2=Green · B3=Red · B4=NIR · B5=SWIR1 · B7=SWIR2</li>
<li>Landsat 8/9 (SR, 6 bandas apiladas): B1=Blue · B2=Green · B3=Red · B4=NIR · B5=SWIR1 · B6=SWIR2</li>
<li>Sentinel-2 (6 bandas apiladas): B1=Blue(B2) · B2=Green(B3) · B3=Red(B4) · B4=NIR(B8) · B5=SWIR1(B11) · B6=SWIR2(B12)</li>
</ul>

<b>Salida:</b> raster GeoTIFF Float32, 3 bandas (Brightness · Greenness · Wetness),
comprimido LZW, con CRS y geotransform de la imagen original.<br><br>

<b>Web:</b> https://www.geomatica.pe/
"""

    # -------------------------------------------------------
    # PARÁMETROS
    # -------------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                "Imagen multiespectral de entrada (BLUE + GREEN + RED + NIR + SWIR1 + SWIR2)"
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.SENSOR_TYPE,
                "Sensor / satélite",
                options=SENSOR_KEYS,
                defaultValue=2,   # Landsat 8 por defecto
                allowMultiple=False
            )
        )

        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_BLUE,
                "Banda BLUE  (azul visible)",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_GREEN,
                "Banda GREEN (verde visible)",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_RED,
                "Banda RED   (rojo visible)",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_NIR,
                "Banda NIR   (infrarrojo cercano)",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_SWIR1,
                "Banda SWIR1 (infrarrojo de onda corta 1  ~1.6 µm)",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_SWIR2,
                "Banda SWIR2 (infrarrojo de onda corta 2  ~2.2 µm)",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_RASTER,
                "Imagen Tasseled Cap (3 bandas: Brightness · Greenness · Wetness)"
            )
        )

    # -------------------------------------------------------
    # PROCESO PRINCIPAL
    # -------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        # ---------------------------------------------------
        # 1. PARÁMETROS
        # ---------------------------------------------------
        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster_layer is None:
            raise QgsProcessingException("❌ No se pudo leer el raster de entrada.")

        input_path  = raster_layer.source()
        sensor_idx  = self.parameterAsEnum(parameters, self.SENSOR_TYPE, context)
        sensor_name = SENSOR_KEYS[sensor_idx]
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT_RASTER, context)

        # Números de banda (1-based, QGIS estándar)
        b_blue  = self.parameterAsInt(parameters, self.BAND_BLUE,  context)
        b_green = self.parameterAsInt(parameters, self.BAND_GREEN, context)
        b_red   = self.parameterAsInt(parameters, self.BAND_RED,   context)
        b_nir   = self.parameterAsInt(parameters, self.BAND_NIR,   context)
        b_swir1 = self.parameterAsInt(parameters, self.BAND_SWIR1, context)
        b_swir2 = self.parameterAsInt(parameters, self.BAND_SWIR2, context)

        band_map = {
            "BLUE":  b_blue,
            "GREEN": b_green,
            "RED":   b_red,
            "NIR":   b_nir,
            "SWIR1": b_swir1,
            "SWIR2": b_swir2,
        }

        feedback.pushInfo("════════════════════════════════════════════════")
        feedback.pushInfo(f"🛰  Sensor       : {sensor_name}")
        feedback.pushInfo(f"📥 Raster entrada: {os.path.basename(input_path)}")
        for rol, num in band_map.items():
            feedback.pushInfo(f"   {rol:<6} → banda {num}")
        feedback.pushInfo("════════════════════════════════════════════════")

        # Validar que no hay bandas repetidas o inválidas
        vals = list(band_map.values())
        if len(set(vals)) < 6:
            raise QgsProcessingException(
                "❌ Hay bandas repetidas en la asignación. "
                "Cada rol espectral debe apuntar a una banda distinta."
            )

        # ---------------------------------------------------
        # 2. APERTURA Y LECTURA DEL RASTER
        # ---------------------------------------------------
        ds = gdal.Open(input_path, gdal.GA_ReadOnly)
        if ds is None:
            raise QgsProcessingException("❌ GDAL no pudo abrir la imagen.")

        cols   = ds.RasterXSize
        rows   = ds.RasterYSize
        nbands = ds.RasterCount
        gt     = ds.GetGeoTransform()
        proj   = ds.GetProjection()

        feedback.pushInfo(f"📐 Dimensiones  : {cols} x {rows} px")
        feedback.pushInfo(f"📦 N° bandas    : {nbands}")

        # Validar que los números de banda existen en el raster
        for rol, num in band_map.items():
            if num < 1 or num > nbands:
                raise QgsProcessingException(
                    f"❌ La banda asignada a {rol} (banda {num}) no existe "
                    f"en el raster (tiene {nbands} banda(s))."
                )

        feedback.pushInfo("📊 Leyendo bandas espectrales...")

        def read_band(band_num):
            """Lee una banda y enmascara nodata con NaN (float64)."""
            b = ds.GetRasterBand(band_num)
            arr = b.ReadAsArray().astype(np.float64)
            nd = b.GetNoDataValue()
            if nd is not None:
                arr[arr == nd] = np.nan
            return arr

        BLUE  = read_band(b_blue);  feedback.setProgress(8)
        GREEN = read_band(b_green); feedback.setProgress(16)
        RED   = read_band(b_red);   feedback.setProgress(24)
        NIR   = read_band(b_nir);   feedback.setProgress(32)
        SWIR1 = read_band(b_swir1); feedback.setProgress(40)
        SWIR2 = read_band(b_swir2); feedback.setProgress(48)

        ds = None

        # ---------------------------------------------------
        # 3. APLICAR COEFICIENTES TASSELED CAP
        # ---------------------------------------------------
        coefs = TC_COEF[sensor_name]
        cB = coefs["Brightness"]
        cG = coefs["Greenness"]
        cW = coefs["Wetness"]

        feedback.pushInfo(f"⚙  Calculando Brightness...")
        Brightness = (
            cB[0]*BLUE + cB[1]*GREEN + cB[2]*RED +
            cB[3]*NIR  + cB[4]*SWIR1 + cB[5]*SWIR2
        )
        feedback.setProgress(60)

        feedback.pushInfo(f"⚙  Calculando Greenness...")
        Greenness = (
            cG[0]*BLUE + cG[1]*GREEN + cG[2]*RED +
            cG[3]*NIR  + cG[4]*SWIR1 + cG[5]*SWIR2
        )
        feedback.setProgress(72)

        feedback.pushInfo(f"⚙  Calculando Wetness...")
        Wetness = (
            cW[0]*BLUE + cW[1]*GREEN + cW[2]*RED +
            cW[3]*NIR  + cW[4]*SWIR1 + cW[5]*SWIR2
        )
        feedback.setProgress(84)

        # Estadísticas rápidas para el log
        for nombre, arr in [("Brightness", Brightness), ("Greenness", Greenness), ("Wetness", Wetness)]:
            vmin = float(np.nanmin(arr))
            vmax = float(np.nanmax(arr))
            vmean = float(np.nanmean(arr))
            feedback.pushInfo(
                f"   {nombre:<12}: min={vmin:.4f}  max={vmax:.4f}  mean={vmean:.4f}"
            )

        # ---------------------------------------------------
        # 4. EXPORTAR RASTER (3 BANDAS, FLOAT32)
        # ---------------------------------------------------
        feedback.pushInfo("💾 Exportando raster Tasseled Cap (3 bandas)...")

        nodata_out = -9999.0
        driver = gdal.GetDriverByName("GTiff")
        ds_out = driver.Create(
            output_path,
            cols,
            rows,
            3,
            gdal.GDT_Float32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out.SetGeoTransform(gt)
        ds_out.SetProjection(proj)

        band_data = [
            ("Brightness", Brightness),
            ("Greenness",  Greenness),
            ("Wetness",    Wetness),
        ]

        for i, (label, arr) in enumerate(band_data, start=1):
            arr_f32 = np.where(np.isnan(arr), nodata_out, arr).astype(np.float32)
            b = ds_out.GetRasterBand(i)
            b.WriteArray(arr_f32)
            b.SetDescription(label)
            b.SetNoDataValue(nodata_out)

        ds_out.FlushCache()
        ds_out = None

        feedback.pushInfo(f"   ✔ Archivo guardado: {output_path}")
        feedback.setProgress(100)

        # ---------------------------------------------------
        # 5. LIMPIEZA
        # ---------------------------------------------------
        del BLUE, GREEN, RED, NIR, SWIR1, SWIR2
        del Brightness, Greenness, Wetness
        gc.collect()

        feedback.pushInfo("════════════════════════════════════════════════")
        feedback.pushInfo("✅ TASSELED CAP COMPLETO")
        feedback.pushInfo(f"   Sensor  : {sensor_name}")
        feedback.pushInfo("   Banda 1 : Brightness (Brillo)")
        feedback.pushInfo("   Banda 2 : Greenness  (Verdor)")
        feedback.pushInfo("   Banda 3 : Wetness    (Humedad)")
        feedback.pushInfo("════════════════════════════════════════════════")

        return {self.OUTPUT_RASTER: output_path}

    def run(self):
        processing.execAlgorithmDialog(self)
