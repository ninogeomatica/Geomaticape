# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 12 - Cobertura del Suelo
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.2

Productos: MCD12Q1 MCD12Q2 MCD12C1
Dato categórico — sin factor de escala numérico.
Remuestreo vecino más cercano  |  Nodata: 255  |  WGS84 EPSG:4326
"""

import os
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing


PREFIJOS_MODIS12 = ["MCD12Q1", "MCD12Q2", "MCD12C1"]

BANDAS_MODIS12 = {
    "LC_Type1" : "_LUC_Type1_IGBP",
    "LC_Type2" : "_LUC_Type2_UMD",
    "LC_Type3" : "_LUC_Type3_LAI",
    "LC_Type4" : "_LUC_Type4_BGC",
    "LC_Type5" : "_LUC_Type5_PFT",
}

FACTOR      = 1.0
OFFSET      = 0.0
NODATA_OUT  = 255
DTYPE_OUT   = "uint8"
RESAMPLE_NN = True   # obligatorio para dato categórico


class FactorMODIS12(QgsProcessingAlgorithm):

    INPUT_FOLDER  = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def name(self):        return "factor_modis12"
    def displayName(self): return "Factor escala MODIS 12 (Cobertura del Suelo)"
    def group(self):       return "Conversion"
    def groupId(self):     return "geomaticape_conversion"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), '..', 'Icons', 'clasificacion.png'))

    def createInstance(self):
        return FactorMODIS12()

    def shortHelpString(self):
        return """
<h3>MODIS 12 — Cobertura del Suelo</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.2<br><br>
<b>Productos:</b>
<ul>
<li>MCD12Q1 → 500 m anual (Terra + Aqua)</li>
<li>MCD12Q2 → 500 m anual (fenología)</li>
<li>MCD12C1 → 0.05° anual</li>
</ul>
<b>Dato categórico</b> — remuestreo vecino más cercano.<br>
<b>Nodata:</b> 255 &nbsp;|&nbsp; <b>Proyección:</b> WGS84 EPSG:4326<br><br>
<b>Capas exportadas:</b><br>
_LUC_Type1_IGBP, _LUC_Type2_UMD, _LUC_Type3_LAI, _LUC_Type4_BGC, _LUC_Type5_PFT<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            "Carpeta con archivos MODIS 12 HDF\n(MCD12Q1, MCD12Q2, MCD12C1)",
            behavior=QgsProcessingParameterFile.Folder
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER,
            "Carpeta de salida GeoTIFF (Clasificación de cobertura)"
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
            for p in PREFIJOS_MODIS12:
                if hdf.upper().startswith(p):
                    prefijo_valido = p
                    break
            if prefijo_valido:
                break

        if not prefijo_valido:
            raise Exception("No se detectó producto MODIS 12. Prefijos válidos: " +
                            ", ".join(PREFIJOS_MODIS12))

        feedback.pushInfo(f"🗺  Producto  : {prefijo_valido}")
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

            for nombre_sds, sufijo in BANDAS_MODIS12.items():

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

        feedback.pushInfo(f"\n{'='*55}")
        feedback.pushInfo(f"✅ MODIS 12 — {len(resultados)} archivo(s) exportado(s)")
        feedback.pushInfo(f"   Carpeta: {carpeta_out}")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
