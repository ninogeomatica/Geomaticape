# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 09 - Reflectancia Superficial
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.2

Productos: MOD09GQ MYD09GQ MOD09Q1 MYD09Q1  →  250m (B1-B2)
           MOD09GA MYD09GA MOD09A1 MYD09A1  →  500m (B1-B7)

Factor: ND × 0.0001
Nodata entrada: -28672  →  salida: -2.8672
Salida: WGS84 EPSG:4326 / GeoTIFF comprimido
"""

import os
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing


PREFIJOS_MODIS09 = [
    "MOD09GQ", "MYD09GQ", "MOD09Q1", "MYD09Q1",
    "MOD09GA", "MYD09GA", "MOD09A1", "MYD09A1",
]

# Clave: fragmento que aparece en la descripción o path del SDS
# Valor: sufijo del archivo de salida
BANDAS_MODIS09 = {
    "sur_refl_b01": "_B1_Red",
    "sur_refl_b02": "_B2_NIR",
    "sur_refl_b03": "_B3_Blue",
    "sur_refl_b04": "_B4_Green",
    "sur_refl_b05": "_B5_SWIR1",
    "sur_refl_b06": "_B6_SWIR2",
    "sur_refl_b07": "_B7_SWIR3",
}

FACTOR      = 0.0001
OFFSET      = 0.0
NODATA_OUT  = -2.8672
DTYPE_OUT   = "float32"
RESAMPLE_NN = False


class FactorMODIS09(QgsProcessingAlgorithm):

    INPUT_FOLDER  = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def name(self):        return "factor_modis09"
    def displayName(self): return "Factor escala MODIS 09 (Reflectancia Superficial)"
    def group(self):       return "Conversion"
    def groupId(self):     return "geomaticape_conversion"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), '..', 'Icons', 'indices.png'))

    def createInstance(self):
        return FactorMODIS09()

    def shortHelpString(self):
        return """
<h3>Factor de escala MODIS 09 — Reflectancia Superficial</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.2<br><br>
<b>Productos:</b>
<ul>
<li>MOD09GQ / MYD09GQ → 250 m diario (B1-B2)</li>
<li>MOD09Q1 / MYD09Q1 → 250 m 8 días (B1-B2)</li>
<li>MOD09GA / MYD09GA → 500 m diario (B1-B7)</li>
<li>MOD09A1 / MYD09A1 → 500 m 8 días (B1-B7)</li>
</ul>
<b>Factor:</b> ND × 0.0001 &nbsp;|&nbsp;
<b>Nodata salida:</b> −2.8672 &nbsp;|&nbsp;
<b>Proyección:</b> WGS84 EPSG:4326<br><br>
<b>Salidas:</b> _B1_Red, _B2_NIR, _B3_Blue, _B4_Green, _B5_SWIR1, _B6_SWIR2, _B7_SWIR3<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            "Carpeta con archivos MODIS 09 HDF\n(MOD09GQ, MYD09GQ, MOD09GA, MOD09A1, ...)",
            behavior=QgsProcessingParameterFile.Folder
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER,
            "Carpeta de salida GeoTIFF (Reflectancia superficial)"
        ))

    # --------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):

        from .  _modis_core import listar_sds, procesar_banda

        carpeta_in  = self.parameterAsString(parameters, self.INPUT_FOLDER,  context)
        carpeta_out = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(carpeta_out, exist_ok=True)

        hdfs = sorted([f for f in os.listdir(carpeta_in) if f.lower().endswith(".hdf")])
        if not hdfs:
            raise Exception("No se encontraron archivos .hdf en la carpeta.")

        # Detectar prefijo
        prefijo_valido = None
        for hdf in hdfs:
            for p in PREFIJOS_MODIS09:
                if hdf.upper().startswith(p):
                    prefijo_valido = p
                    break
            if prefijo_valido:
                break

        if not prefijo_valido:
            raise Exception("No se detectó producto MODIS 09. Prefijos válidos: " +
                            ", ".join(PREFIJOS_MODIS09))

        es_250m = prefijo_valido in ["MOD09GQ","MYD09GQ","MOD09Q1","MYD09Q1"]
        bandas_activas = {
            k: v for k, v in BANDAS_MODIS09.items()
            if not es_250m or k in ("sur_refl_b01", "sur_refl_b02")
        }

        feedback.pushInfo(f"🌍 Producto  : {prefijo_valido}")
        feedback.pushInfo(f"📐 Resolución: {'250 m (B1-B2)' if es_250m else '500 m (B1-B7)'}")
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
            for sp, sd in sds_lista:
                feedback.pushInfo(f"     {sp.split(':')[-1]}")

            for nombre_sds, sufijo in bandas_activas.items():

                # Buscar el SDS cuyo PATH contiene el nombre de banda
                sds_path = next(
                    (sp for sp, _ in sds_lista if nombre_sds in sp),
                    None
                )
                if sds_path is None:
                    feedback.pushInfo(f"  ⏭ '{nombre_sds}' no encontrado")
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
        feedback.pushInfo(f"✅ MODIS 09 — {len(resultados)} archivo(s) exportado(s)")
        feedback.pushInfo(f"   Carpeta: {carpeta_out}")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
