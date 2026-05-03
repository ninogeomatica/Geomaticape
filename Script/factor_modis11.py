# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 11 - LST (Land Surface Temperature)
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.2

Productos: MOD11A1 MYD11A1 MOD11A2 MYD11A2 MOD11B1 MYD11B1
           MOD11C1 MYD11C1 MOD11C2 MYD11C2 MOD11C3 MYD11C3

LST(°C) = ND × 0.02 − 273.15
Nodata salida: -9999  |  WGS84 EPSG:4326
"""

import os
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing


PREFIJOS_MODIS11 = [
    "MOD11A1","MYD11A1","MOD11A2","MYD11A2",
    "MOD11B1","MYD11B1","MOD11C1","MYD11C1",
    "MOD11C2","MYD11C2","MOD11C3","MYD11C3",
]

BANDAS_MODIS11 = {
    "LST_Day_1km"   : "_LST_Day_C",
    "LST_Night_1km" : "_LST_Night_C",
    "LST_Day_6km"   : "_LST_Day_C",
    "LST_Night_6km" : "_LST_Night_C",
    "LST_Day_CMG"   : "_LST_Day_C",
    "LST_Night_CMG" : "_LST_Night_C",
}

FACTOR      = 0.02
OFFSET      = -273.15
NODATA_OUT  = -9999.0
DTYPE_OUT   = "float32"
RESAMPLE_NN = False


class FactorMODIS11(QgsProcessingAlgorithm):

    INPUT_FOLDER  = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def name(self):        return "factor_modis11"
    def displayName(self): return "Factor escala MODIS 11 (LST °C)"
    def group(self):       return "Conversion"
    def groupId(self):     return "geomaticape_conversion"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), '..', 'Icons', 'indices.png'))

    def createInstance(self):
        return FactorMODIS11()

    def shortHelpString(self):
        return """
<h3>Factor de escala MODIS 11 — LST Temperatura Superficial</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.2<br><br>
<b>Productos:</b>
<ul>
<li>MOD11A1 / MYD11A1 → 1000 m diario</li>
<li>MOD11A2 / MYD11A2 → 1000 m 8 días</li>
<li>MOD11B1 / MYD11B1 → 6000 m diario</li>
<li>MOD11C1-C3 / MYD11C1-C3 → 0.05° diario/8días/mensual</li>
</ul>
<b>Fórmula:</b> LST(°C) = ND × 0.02 − 273.15<br>
<b>Nodata salida:</b> −9999 &nbsp;|&nbsp;
<b>Proyección:</b> WGS84 EPSG:4326<br><br>
<b>Salidas:</b> _LST_Day_C.tif, _LST_Night_C.tif<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            "Carpeta con archivos MODIS 11 HDF\n(MOD11A1, MYD11A1, MOD11A2, ...)",
            behavior=QgsProcessingParameterFile.Folder
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER,
            "Carpeta de salida GeoTIFF (LST °C)"
        ))

    # --------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):

        from ._modis_core import listar_sds, procesar_banda

        carpeta_in  = self.parameterAsString(parameters, self.INPUT_FOLDER,  context)
        carpeta_out = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(carpeta_out, exist_ok=True)

        hdfs = sorted([f for f in os.listdir(carpeta_in) if f.lower().endswith(".hdf")])
        if not hdfs:
            raise Exception("No se encontraron archivos .hdf en la carpeta.")

        prefijo_valido = None
        for hdf in hdfs:
            for p in PREFIJOS_MODIS11:
                if hdf.upper().startswith(p):
                    prefijo_valido = p
                    break
            if prefijo_valido:
                break

        if not prefijo_valido:
            raise Exception("No se detectó producto MODIS 11. Prefijos válidos: " +
                            ", ".join(PREFIJOS_MODIS11))

        feedback.pushInfo(f"🌡  Producto  : {prefijo_valido}")
        feedback.pushInfo(f"📄 HDF totales: {len(hdfs)}")

        resultados     = []
        sufijos_vistos = set()

        for idx, hdf in enumerate(hdfs):
            if feedback.isCanceled():
                break

            ruta_hdf    = os.path.join(carpeta_in, hdf)
            nombre_base = os.path.splitext(hdf)[0][:23]

            feedback.pushInfo(f"\n{'─'*55}")
            feedback.pushInfo(f"📄 ({idx+1}/{len(hdfs)}) {hdf}")
            feedback.setProgress(int(idx / len(hdfs) * 100))

            sds_lista = listar_sds(ruta_hdf, feedback)
            if not sds_lista:
                continue

            feedback.pushInfo("  📋 Subdatasets disponibles:")
            for sp, _ in sds_lista:
                feedback.pushInfo(f"     {sp.split(':')[-1]}")

            sufijos_vistos.clear()

            for nombre_sds, sufijo in BANDAS_MODIS11.items():

                if sufijo in sufijos_vistos:
                    continue

                sds_path = next(
                    (sp for sp, _ in sds_lista if nombre_sds in sp),
                    None
                )
                if sds_path is None:
                    continue

                nombre_tif = nombre_base + sufijo + ".tif"
                ruta_tif   = os.path.join(carpeta_out, nombre_tif)

                if os.path.exists(ruta_tif):
                    feedback.pushInfo(f"  ⏩ Ya existe: {nombre_tif}")
                    resultados.append(ruta_tif)
                    sufijos_vistos.add(sufijo)
                    continue

                feedback.pushInfo(f"  ➤ {nombre_sds}  →  {nombre_tif}")

                ok = procesar_banda(
                    sds_path    = sds_path,
                    ruta_tif_out= ruta_tif,
                    factor      = FACTOR,
                    offset      = OFFSET,
                    nodata_out  = NODATA_OUT,
                    dtype_out   = DTYPE_OUT,
                    resample_nn = RESAMPLE_NN,
                    feedback    = feedback
                )
                if ok:
                    feedback.pushInfo(f"     ✔ {nombre_tif}")
                    resultados.append(ruta_tif)
                    sufijos_vistos.add(sufijo)

        feedback.pushInfo(f"\n{'='*55}")
        feedback.pushInfo(f"✅ MODIS 11 — {len(resultados)} archivo(s) exportado(s)")
        feedback.pushInfo(f"   Carpeta: {carpeta_out}")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
