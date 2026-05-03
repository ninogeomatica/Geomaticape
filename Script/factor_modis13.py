# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 13 - Índices de Vegetación (NDVI / EVI)
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.2

Productos: MOD13Q1 MYD13Q1  → 250m / 16d
           MOD13A1 MYD13A1  → 500m / 16d
           MOD13A2 MYD13A2  → 1km  / 16d
           MOD13A3 MYD13A3  → 1km  / mensual
           MOD13C1 MYD13C1  → 5.6km / 16d
           MOD13C2 MYD13C2  → 5.6km / mensual

Factor: ND × 0.0001  |  Nodata salida: -0.3  |  WGS84 EPSG:4326
"""

import os
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing


PREFIJOS_MODIS13 = [
    "MOD13Q1","MYD13Q1","MOD13A1","MYD13A1",
    "MOD13A2","MYD13A2","MOD13A3","MYD13A3",
    "MOD13C1","MYD13C1","MOD13C2","MYD13C2",
]

# Todas las variantes de nombre de SDS para NDVI y EVI según sub-producto
BANDAS_MODIS13 = {
    "250m 16 days NDVI"         : "_NDVI",
    "500m 16 days NDVI"         : "_NDVI",
    "1 km 16 days NDVI"         : "_NDVI",
    "CMG 0.05 Deg 16 days NDVI" : "_NDVI",
    "CMG 0.05 Deg Monthly NDVI" : "_NDVI",
    "1 km monthly NDVI"         : "_NDVI",
    "250m 16 days EVI"          : "_EVI",
    "500m 16 days EVI"          : "_EVI",
    "1 km 16 days EVI"          : "_EVI",
    "CMG 0.05 Deg 16 days EVI"  : "_EVI",
    "CMG 0.05 Deg Monthly EVI"  : "_EVI",
    "1 km monthly EVI"          : "_EVI",
}

FACTOR      = 0.0001
OFFSET      = 0.0
NODATA_OUT  = -0.3
DTYPE_OUT   = "float32"
RESAMPLE_NN = False


class FactorMODIS13(QgsProcessingAlgorithm):

    INPUT_FOLDER  = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def name(self):        return "factor_modis13"
    def displayName(self): return "Factor escala MODIS 13 (NDVI / EVI)"
    def group(self):       return "Conversion"
    def groupId(self):     return "geomaticape_conversion"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), '..', 'Icons', 'indices.png'))

    def createInstance(self):
        return FactorMODIS13()

    def shortHelpString(self):
        return """
<h3>Factor de escala MODIS 13 — NDVI / EVI</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.2<br><br>
<b>Productos:</b>
<ul>
<li>MOD13Q1 / MYD13Q1 → 250 m / 16 días</li>
<li>MOD13A1 / MYD13A1 → 500 m / 16 días</li>
<li>MOD13A2 / MYD13A2 → 1000 m / 16 días</li>
<li>MOD13A3 / MYD13A3 → 1000 m / mensual</li>
<li>MOD13C1-C2 / MYD13C1-C2 → 5600 m / 16días-mensual</li>
</ul>
<b>Factor:</b> ND × 0.0001 &nbsp;|&nbsp;
<b>Nodata salida:</b> −0.3 &nbsp;|&nbsp;
<b>Proyección:</b> WGS84 EPSG:4326<br><br>
<b>Salidas:</b> _NDVI.tif, _EVI.tif<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            "Carpeta con archivos MODIS 13 HDF\n(MOD13Q1, MYD13Q1, MOD13A1, MOD13A2, ...)",
            behavior=QgsProcessingParameterFile.Folder
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER,
            "Carpeta de salida GeoTIFF (NDVI / EVI)"
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
            for p in PREFIJOS_MODIS13:
                if hdf.upper().startswith(p):
                    prefijo_valido = p
                    break
            if prefijo_valido:
                break

        if not prefijo_valido:
            raise Exception("No se detectó producto MODIS 13. Prefijos válidos: " +
                            ", ".join(PREFIJOS_MODIS13))

        feedback.pushInfo(f"🛰  Producto  : {prefijo_valido}")
        feedback.pushInfo(f"📄 HDF totales: {len(hdfs)}")

        resultados = []

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

            sufijos_vistos = set()

            for nombre_sds, sufijo in BANDAS_MODIS13.items():

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
        feedback.pushInfo(f"✅ MODIS 13 — {len(resultados)} archivo(s) exportado(s)")
        feedback.pushInfo(f"   Carpeta: {carpeta_out}")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
