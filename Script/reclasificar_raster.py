"""
Reclasificar Raster
===================
Modifica los valores categoricos de un raster clasificado mediante una
tabla interactiva de remapeo (valor_anterior -> nuevo_valor).

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
    QgsProcessingParameterBoolean,
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


class ReclasificarRaster(QgsProcessingAlgorithm):

    INPUT_RASTER  = "INPUT_RASTER"
    BAND          = "BAND"
    TABLE         = "TABLE"
    KEEP_UNMAPPED = "KEEP_UNMAPPED"
    DTYPE         = "DTYPE"
    OUTPUT_RASTER = "OUTPUT_RASTER"

    def name(self):
        return "reclasificar_raster"

    def displayName(self):
        return "Reclasificar raster (remapeo de valores)"

    def group(self):
        return "PostProcesamiento"

    def groupId(self):
        return "geomaticape_postprocesamiento"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..", "Icons", "clasificacion.png"))

    def createInstance(self):
        return ReclasificarRaster()

    def shortHelpString(self):
        return """
<h3>Reclasificar Raster</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.1<br><br>

<b>Descripcion:</b><br>
Modifica los valores categoricos de un raster clasificado mapeando cada
valor anterior a un nuevo valor mediante una tabla interactiva.<br><br>

<b>Tabla de remapeo — columnas:</b>
<ul>
<li><b>Valor anterior</b>: valor existente en el raster clasificado</li>
<li><b>Nuevo valor</b>: valor que reemplazara al anterior</li>
</ul>

<b>Casos de uso tipicos:</b>
<ul>
<li>Fusionar clases: clases 3 y 4 -> clase 3 "Vegetacion"</li>
<li>Recodificar leyenda para compatibilidad con otra clasificacion</li>
<li>Eliminar clases no deseadas (asignarles NoData)</li>
<li>Invertir o reorganizar la escala de valores</li>
</ul>

<b>Valores no mapeados:</b><br>
Si activas <b>Conservar valores no mapeados</b>, los pixeles que no
aparezcan en la tabla mantienen su valor original. Si esta desactivado,
se convierten en NoData.<br><br>

<b>Ejemplo — fusionar vegetacion escasa (3) y moderada (4):</b><br>
<pre>
Valor anterior | Nuevo valor
1              | 1    (Agua)
2              | 2    (Suelo)
3              | 3    (Vegetacion)
4              | 3    (Vegetacion)
5              | 4    (Veg. densa)
</pre>

<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                "Raster clasificado de entrada"
            )
        )

        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND,
                "Banda a reclasificar",
                parentLayerParameterName=self.INPUT_RASTER,
                optional=False
            )
        )

        self.addParameter(
            QgsProcessingParameterMatrix(
                self.TABLE,
                "Tabla de remapeo  (Valor anterior | Nuevo valor)",
                headers=["Valor anterior", "Nuevo valor"],
                defaultValue=[
                    1, 1,
                    2, 2,
                    3, 3,
                    4, 3,
                    5, 4,
                ]
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.KEEP_UNMAPPED,
                "Conservar valores no mapeados (desactivado -> NoData)",
                defaultValue=False
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
                "Raster reclasificado"
            )
        )

    def processAlgorithm(self, parameters, context, feedback):

        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster_layer is None:
            raise QgsProcessingException("No se pudo leer el raster de entrada.")

        input_path    = raster_layer.source()
        band_num      = self.parameterAsInt(parameters, self.BAND, context)
        flat          = self.parameterAsMatrix(parameters, self.TABLE, context)
        keep_unmapped = self.parameterAsBool(parameters, self.KEEP_UNMAPPED, context)
        dtype_idx     = self.parameterAsEnum(parameters, self.DTYPE, context)
        output_path   = self.parameterAsOutputLayer(parameters, self.OUTPUT_RASTER, context)

        gdal_dtype = DTYPE_GDAL[dtype_idx]
        np_dtype   = DTYPE_NP[dtype_idx]
        nodata_out = NODATA_BY_DTYPE[dtype_idx]

        # --- Parseo de la tabla (lista plana -> filas de 2 columnas) ---
        if len(flat) == 0:
            raise QgsProcessingException("La tabla de remapeo esta vacia. Agregue al menos una fila.")
        if len(flat) % 2 != 0:
            raise QgsProcessingException(
                "La tabla debe tener exactamente 2 columnas (Valor anterior, Nuevo valor). "
                "Verifique que no haya filas incompletas."
            )

        rules    = []
        seen_old = set()
        for i in range(0, len(flat), 2):
            try:
                v_old = float(flat[i])
                v_new = float(flat[i + 1])
            except (TypeError, ValueError):
                raise QgsProcessingException(
                    "Fila {}: valores no numericos -> {}".format(i // 2 + 1, flat[i:i+2])
                )
            if v_old in seen_old:
                raise QgsProcessingException(
                    "Fila {}: el valor {} aparece mas de una vez en 'Valor anterior'. "
                    "Cada valor debe mapearse exactamente una vez.".format(i // 2 + 1, v_old)
                )
            seen_old.add(v_old)
            rules.append({"old": v_old, "new": v_new})

        feedback.pushInfo("=" * 50)
        feedback.pushInfo("Raster entrada      : {}".format(os.path.basename(input_path)))
        feedback.pushInfo("Banda               : {}".format(band_num))
        feedback.pushInfo("Reglas de remapeo   : {}".format(len(rules)))
        feedback.pushInfo("Conservar no mapeados: {}".format("Si" if keep_unmapped else "No -> NoData"))
        feedback.pushInfo("-" * 50)
        for r in rules:
            feedback.pushInfo("  {:>8.2f}  ->  {:>8.2f}".format(r["old"], r["new"]))
        feedback.pushInfo("=" * 50)

        # --- Lectura ---
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
        nodata_mask = np.zeros(arr.shape, dtype=bool)
        if nd is not None:
            nodata_mask = (arr == nd)
        ds = None
        feedback.setProgress(25)

        # --- Remapeo ---
        feedback.pushInfo("Aplicando tabla de remapeo...")

        if keep_unmapped:
            resultado = arr.astype(np_dtype)
        else:
            resultado = np.full(arr.shape, nodata_out, dtype=np_dtype)

        for i, r in enumerate(rules):
            mask = (arr == r["old"]) & ~nodata_mask
            resultado[mask] = np_dtype(r["new"])
            feedback.setProgress(25 + int((i + 1) / len(rules) * 60))

        resultado[nodata_mask] = np_dtype(nodata_out)

        # --- Log ---
        pixel_area_m2 = abs(gt[1]) * abs(gt[5])
        new_vals = sorted(set(r["new"] for r in rules))
        feedback.pushInfo("Pixeles por nuevo valor:")
        for v in new_vals:
            n_pix   = int(np.sum(resultado == np_dtype(v)))
            area_ha = n_pix * pixel_area_m2 / 10_000
            feedback.pushInfo("  Valor {:>4.0f} -> {:>12,} px  |  {:>12,.4f} ha".format(
                v, n_pix, area_ha
            ))
        feedback.setProgress(87)

        # --- Exportar ---
        feedback.pushInfo("Exportando raster reclasificado...")
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
        band_out.SetDescription("Raster reclasificado")
        ds_out.FlushCache()
        ds_out = None

        del arr, resultado
        gc.collect()

        feedback.pushInfo("Guardado: {}".format(output_path))
        feedback.setProgress(100)
        feedback.pushInfo("RECLASIFICACION COMPLETA")

        return {self.OUTPUT_RASTER: output_path}

    def run(self):
        processing.execAlgorithmDialog(self)
