"""
Clasificar Raster por Rangos
============================
Clasifica un raster continuo asignando un valor de clase a cada píxel
según rangos min-max definidos en una tabla interactiva.

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.1
Grupo : PostProcesamiento
"""

import os
import gc
import numpy as np

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterBand,
    QgsProcessingParameterEnum,
    QgsProcessingParameterMatrix,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException,
)
from qgis import processing
from osgeo import gdal


DTYPE_OPTIONS = [
    "Int16   - entero con signo  (recomendado para clases categoricas)",
    "UInt8   - entero sin signo  (0-255, clases pequeñas)",
    "UInt16  - entero sin signo  (0-65535, muchas clases)",
    "Int32   - entero 32-bit     (valores grandes o negativos)",
    "Float32 - punto flotante    (cuando el valor de clase no es entero)",
]
DTYPE_GDAL = [gdal.GDT_Int16, gdal.GDT_Byte, gdal.GDT_UInt16, gdal.GDT_Int32, gdal.GDT_Float32]
DTYPE_NP   = [np.int16, np.uint8, np.uint16, np.int32, np.float32]
NODATA_BY_DTYPE = [-9999, 255, 65535, -9999, -9999.0]


class ClasificarRaster(QgsProcessingAlgorithm):

    INPUT_RASTER  = "INPUT_RASTER"
    BAND          = "BAND"
    TABLE         = "TABLE"
    DTYPE         = "DTYPE"
    OUTPUT_RASTER = "OUTPUT_RASTER"

    def name(self):
        return "clasificar_raster"

    def displayName(self):
        return "Clasificar raster por rangos"

    def group(self):
        return "PostProcesamiento"

    def groupId(self):
        return "geomaticape_postprocesamiento"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..", "Icons", "clasificacion.png"))

    def createInstance(self):
        return ClasificarRaster()

    def shortHelpString(self):
        return """
<h3>Clasificar Raster por Rangos</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.1<br><br>

<b>Descripcion:</b><br>
Asigna un valor de clase a cada pixel segun reglas de rango definidas
en la tabla interactiva. Cada fila define:<br>
<ul>
<li><b>Minimo</b>: valor minimo del rango (inclusivo)</li>
<li><b>Maximo</b>: valor maximo del rango (exclusivo, excepto el ultimo rango)</li>
<li><b>Valor salida</b>: valor que se asigna al pixel de esa clase</li>
</ul>

Los pixeles que no caen en ningun rango se marcan como NoData.<br><br>

<b>Ejemplo NDVI (reflectancia 0-1):</b><br>
<pre>
Minimo  | Maximo | Valor salida
-1.0    |  0.0   |  1   (Agua)
 0.0    |  0.15  |  2   (Suelo desnudo)
 0.15   |  0.30  |  3   (Veg. escasa)
 0.30   |  0.60  |  4   (Veg. moderada)
 0.60   |  1.0   |  5   (Veg. densa)
</pre>

<b>Tipo de dato de salida:</b><br>
Usa <b>Int16</b> para clases enteras (recomendado).
Float32 solo si el valor de clase tiene decimales.<br><br>

<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                "Raster a clasificar (NDVI, indices, DEM, temperatura, etc.)"
            )
        )

        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND,
                "Banda a clasificar",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )

        self.addParameter(
            QgsProcessingParameterMatrix(
                self.TABLE,
                "Tabla de clasificacion  (Minimo | Maximo | Valor salida)",
                headers=["Minimo", "Maximo", "Valor salida"],
                defaultValue=[
                    -1.0,  0.0,  1,
                     0.0,  0.15, 2,
                     0.15, 0.30, 3,
                     0.30, 0.60, 4,
                     0.60, 1.0,  5,
                ]
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.DTYPE,
                "Tipo de dato de salida",
                options=DTYPE_OPTIONS,
                defaultValue=0,
                allowMultiple=False
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_RASTER,
                "Raster clasificado"
            )
        )

    def processAlgorithm(self, parameters, context, feedback):

        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster_layer is None:
            raise QgsProcessingException("No se pudo leer el raster de entrada.")

        input_path  = raster_layer.source()
        band_num    = self.parameterAsInt(parameters, self.BAND, context)
        flat        = self.parameterAsMatrix(parameters, self.TABLE, context)
        dtype_idx   = self.parameterAsEnum(parameters, self.DTYPE, context)
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT_RASTER, context)

        gdal_dtype = DTYPE_GDAL[dtype_idx]
        np_dtype   = DTYPE_NP[dtype_idx]
        nodata_out = NODATA_BY_DTYPE[dtype_idx]

        # --- Parseo de la tabla (lista plana -> filas de 3 columnas) ---
        if len(flat) == 0:
            raise QgsProcessingException("La tabla de clasificacion esta vacia. Agregue al menos una fila.")
        if len(flat) % 3 != 0:
            raise QgsProcessingException(
                "La tabla debe tener exactamente 3 columnas (Minimo, Maximo, Valor salida). "
                "Verifique que no haya filas incompletas."
            )

        rules = []
        for i in range(0, len(flat), 3):
            try:
                vmin = float(flat[i])
                vmax = float(flat[i + 1])
                vout = float(flat[i + 2])
            except (TypeError, ValueError):
                raise QgsProcessingException(
                    "Fila {}: valores no numericos -> {}".format(i // 3 + 1, flat[i:i+3])
                )
            if vmin >= vmax:
                raise QgsProcessingException(
                    "Fila {}: Minimo ({}) debe ser menor que Maximo ({}).".format(
                        i // 3 + 1, vmin, vmax
                    )
                )
            rules.append({"min": vmin, "max": vmax, "value": vout})

        rules.sort(key=lambda r: r["min"])

        feedback.pushInfo("=" * 50)
        feedback.pushInfo("Raster entrada : {}".format(os.path.basename(input_path)))
        feedback.pushInfo("Banda          : {}".format(band_num))
        feedback.pushInfo("Tipo de dato   : {}".format(DTYPE_OPTIONS[dtype_idx].split("-")[0].strip()))
        feedback.pushInfo("Reglas         : {}".format(len(rules)))
        feedback.pushInfo("-" * 50)
        for idx, r in enumerate(rules):
            inc = "<=" if idx == len(rules) - 1 else "<"
            feedback.pushInfo("  Clase {:>2}: {:>10.4f} <= x {} {:>10.4f}  ->  {}".format(
                idx + 1, r["min"], inc, r["max"], r["value"]
            ))
        feedback.pushInfo("=" * 50)

        # --- Apertura GDAL ---
        ds = gdal.Open(input_path, gdal.GA_ReadOnly)
        if ds is None:
            raise QgsProcessingException("GDAL no pudo abrir la imagen.")

        cols   = ds.RasterXSize
        rows   = ds.RasterYSize
        nbands = ds.RasterCount
        gt     = ds.GetGeoTransform()
        proj   = ds.GetProjection()

        if band_num < 1 or band_num > nbands:
            raise QgsProcessingException(
                "Banda {} no existe (el raster tiene {} banda(s)).".format(band_num, nbands)
            )

        feedback.pushInfo("Dimensiones: {} x {} px".format(cols, rows))

        b   = ds.GetRasterBand(band_num)
        arr = b.ReadAsArray().astype(np.float64)
        nd  = b.GetNoDataValue()
        if nd is not None:
            arr[arr == nd] = np.nan
        ds = None
        feedback.setProgress(28)

        # --- Clasificacion ---
        feedback.pushInfo("Aplicando clasificacion por rangos...")
        resultado = np.full(arr.shape, nodata_out, dtype=np_dtype)
        nan_mask  = np.isnan(arr)
        n_rules   = len(rules)

        for i, r in enumerate(rules):
            if i == n_rules - 1:
                mask = (arr >= r["min"]) & (arr <= r["max"]) & ~nan_mask
            else:
                mask = (arr >= r["min"]) & (arr <  r["max"]) & ~nan_mask
            resultado[mask] = np_dtype(r["value"])
            feedback.setProgress(28 + int((i + 1) / n_rules * 55))

        # --- Log resultados ---
        pixel_area_m2 = abs(gt[1]) * abs(gt[5])
        feedback.pushInfo("Resultado por clase:")
        for r in rules:
            n_pix   = int(np.sum(resultado == np_dtype(r["value"])))
            area_ha = n_pix * pixel_area_m2 / 10_000
            feedback.pushInfo("  Valor {:>4.0f} -> {:>12,} px  |  {:>12,.4f} ha".format(
                r["value"], n_pix, area_ha
            ))
        feedback.setProgress(85)

        # --- Exportar ---
        feedback.pushInfo("Exportando raster clasificado...")
        driver = gdal.GetDriverByName("GTiff")
        ds_out = driver.Create(
            output_path, cols, rows, 1, gdal_dtype,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out.SetGeoTransform(gt)
        ds_out.SetProjection(proj)

        band_out = ds_out.GetRasterBand(1)
        band_out.WriteArray(resultado)
        band_out.SetNoDataValue(float(nodata_out))
        band_out.SetDescription("Raster clasificado por rangos")

        palette = [
            (68,  1,  84), (59,  82, 139), (33, 145, 140),
            (94, 201,  98), (253, 231,  37), (200,  80,  60),
            (130,  60, 160), (255, 160,   0),
        ]
        ct = gdal.ColorTable()
        for i, r in enumerate(rules):
            v = int(r["value"])
            if 0 <= v <= 65535:
                c = palette[i % len(palette)]
                ct.SetColorEntry(v, (c[0], c[1], c[2], 255))
        band_out.SetColorTable(ct)

        ds_out.FlushCache()
        ds_out = None

        del arr, resultado
        gc.collect()

        feedback.pushInfo("Guardado: {}".format(output_path))
        feedback.setProgress(100)
        feedback.pushInfo("CLASIFICACION COMPLETA")

        return {self.OUTPUT_RASTER: output_path}

    def run(self):
        processing.execAlgorithmDialog(self)
