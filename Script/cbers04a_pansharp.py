import os
import tempfile
import shutil
import gc
import numpy as np
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterRasterDestination,
)
from qgis import processing
from osgeo import gdal, ogr, osr


class CBERS04APansharp(QgsProcessingAlgorithm):

    INPUT_FOLDER = "INPUT_FOLDER"
    INPUT_AOI    = "INPUT_AOI"
    OUTPUT_MS    = "OUTPUT_MS"
    OUTPUT_PS    = "OUTPUT_PS"

    # -------------------------------------------------------
    # IDENTIFICACIÓN
    # -------------------------------------------------------

    def name(self):
        return "cbers04a_pansharp"

    def displayName(self):
        return "CBERS-04A Pansharpening Brovey 2m"

    def group(self):
        return "Procesamiento"

    def groupId(self):
        return "geomaticape_procesamiento"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), "..", "Icons", "cbers04a.png"))

    def createInstance(self):
        return CBERS04APansharp()

    # -------------------------------------------------------
    # AYUDA
    # -------------------------------------------------------

    def shortHelpString(self):
        return """
<h3>CBERS-04A Pansharpening Brovey 2m</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Versión:</b> 1.3<br><br>

<b>Descripción:</b><br>
Aplica fusión pancromática (Brovey) sobre imágenes CBERS-04A WPM
para generar una imagen multiespectral a 2m de resolución espacial.

<b>Satélite compatible:</b>
<ul>
<li>✔ CBERS-04A WPM (Pancromático BAND0 + MS BAND1-4)</li>
</ul>

<b>Proceso:</b>
<ul>
<li>✔ Detecta automáticamente BAND0 (PAN) y BAND1–BAND4 (MS)</li>
<li>✔ Exporta AOI a shapefile temporal y reproyecta al CRS del PAN</li>
<li>✔ Recorta PAN y MS con gdal.Warp + cutlineDSName</li>
<li>✔ Resamplea MS a resolución exacta del PAN (Bilineal)</li>
<li>✔ Aplica fusión Brovey: PS = (MS / ΣMS) × PAN</li>
<li>✔ Exporta MS recortado y PS pansharpenizado (float32, LZW)</li>
<li>✔ Asigna nombres de banda internos</li>
<li>✔ Elimina temporales automáticamente</li>
</ul>

<b>Bandas de salida (MS y PS):</b><br>
B1_Blue · B2_Green · B3_Red · B4_Nir

<b>Requisito:</b> Escena CBERS-04A — TIF con sufijo BAND0, BAND1, BAND2, BAND3, BAND4<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    # -------------------------------------------------------
    # PARÁMETROS
    # -------------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_FOLDER,
                "Carpeta de escena CBERS-04A",
                behavior=QgsProcessingParameterFile.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_AOI,
                "AOI — Área de recorte (shapefile polígono)",
                optional=False
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_MS,
                "MS recortado (4 bandas, resolución 8m)"
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_PS,
                "Imagen Pansharpenizada Brovey 2m"
            )
        )

    # -------------------------------------------------------
    # PROCESO PRINCIPAL
    # -------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        folder    = self.parameterAsString(parameters, self.INPUT_FOLDER, context)
        aoi_layer = self.parameterAsVectorLayer(parameters, self.INPUT_AOI, context)
        output_ms = self.parameterAsOutputLayer(parameters, self.OUTPUT_MS, context)
        output_ps = self.parameterAsOutputLayer(parameters, self.OUTPUT_PS, context)

        # ---------------------------------------------------
        # 1. DETECTAR ARCHIVOS CBERS-04A
        # ---------------------------------------------------
        feedback.pushInfo("🔍 Buscando archivos CBERS-04A...")

        archivos = [f for f in os.listdir(folder) if f.lower().endswith(".tif")]

        pan_file = None
        ms_files = {}

        for f in archivos:
            nombre = f.upper()
            if "BAND0" in nombre:
                pan_file = f
            for i in range(1, 5):
                if f"BAND{i}" in nombre:
                    ms_files[i] = f

        if pan_file is None:
            raise Exception("❌ No se encontró banda pancromática (BAND0).")

        bandas_faltantes = [i for i in range(1, 5) if i not in ms_files]
        if bandas_faltantes:
            raise Exception(f"❌ Faltan bandas multiespectrales: {bandas_faltantes}")

        pan_path = os.path.join(folder, pan_file)
        ms_paths = [os.path.join(folder, ms_files[i]) for i in range(1, 5)]

        base_name = pan_file.upper().split("_BAND")[0]
        feedback.pushInfo(f"🛰 Escena: {base_name}")
        feedback.pushInfo(f"   PAN  → {pan_file}")
        for i in range(1, 5):
            feedback.pushInfo(f"   MS B{i} → {ms_files[i]}")

        # ---------------------------------------------------
        # 2. LEER CRS DEL PAN
        # ---------------------------------------------------
        feedback.pushInfo("📐 Leyendo CRS del PAN...")

        ds_pan      = gdal.Open(pan_path)
        pan_crs_wkt = ds_pan.GetProjection()
        ds_pan      = None

        srs_pan = osr.SpatialReference()
        srs_pan.ImportFromWkt(pan_crs_wkt)
        srs_pan.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        aoi_crs = aoi_layer.crs()
        srs_aoi = osr.SpatialReference()
        srs_aoi.ImportFromWkt(aoi_crs.toWkt())
        srs_aoi.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        # ---------------------------------------------------
        # 3. DIRECTORIO TEMPORAL + SHAPEFILE DE CORTE
        # ---------------------------------------------------
        temp_dir = tempfile.mkdtemp(prefix="cbers04a_")
        feedback.pushInfo(f"📂 Temporal: {temp_dir}")

        # Reproyectar geometrías del AOI al CRS del PAN
        transform_obj = osr.CoordinateTransformation(srs_aoi, srs_pan)

        geom_list = []
        for feat in aoi_layer.getFeatures():
            ogr_geom = ogr.CreateGeometryFromWkt(feat.geometry().asWkt())
            ogr_geom.Transform(transform_obj)
            geom_list.append(ogr_geom.ExportToWkt())

        if not geom_list:
            raise Exception("❌ El AOI no tiene geometrías válidas.")

        # Exportar a shapefile temporal (gdal.Warp Python solo acepta ruta, no objeto en memoria)
        aoi_shp    = os.path.join(temp_dir, "aoi_cut.shp")
        shp_driver = ogr.GetDriverByName("ESRI Shapefile")
        shp_ds     = shp_driver.CreateDataSource(aoi_shp)
        shp_layer  = shp_ds.CreateLayer("aoi_cut", srs_pan, ogr.wkbPolygon)

        for wkt in geom_list:
            feat_out = ogr.Feature(shp_layer.GetLayerDefn())
            feat_out.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
            shp_layer.CreateFeature(feat_out)

        shp_ds.FlushCache()
        shp_ds = None  # Cerrar antes de usar en Warp

        feedback.pushInfo(f"   AOI shapefile: {aoi_shp}")

        # ---------------------------------------------------
        # 4. RECORTAR BANDA PAN
        # ---------------------------------------------------
        feedback.pushInfo("✂ Recortando PAN...")

        pan_clip_path = os.path.join(temp_dir, "pan_clip.tif")

        result = gdal.Warp(
            pan_clip_path,
            pan_path,
            cutlineDSName=aoi_shp,
            cutlineLayer="aoi_cut",
            cropToCutline=True,
            dstNodata=0,
            outputType=gdal.GDT_Float32,
            resampleAlg=gdal.GRA_Bilinear
        )

        if result is None or not os.path.exists(pan_clip_path):
            raise Exception("❌ Error al recortar PAN. Verifica que el AOI se solape con la imagen.")

        result = None  # Liberar

        ds_pan_clip = gdal.Open(pan_clip_path)
        pan_arr     = ds_pan_clip.GetRasterBand(1).ReadAsArray().astype(np.float32)
        pan_gt_clip = ds_pan_clip.GetGeoTransform()
        pan_proj    = ds_pan_clip.GetProjection()
        rows_pan    = ds_pan_clip.RasterYSize
        cols_pan    = ds_pan_clip.RasterXSize
        ds_pan_clip = None

        # Calcular extent exacto del PAN recortado
        pan_xmin = pan_gt_clip[0]
        pan_ymax = pan_gt_clip[3]
        pan_xmax = pan_xmin + pan_gt_clip[1] * cols_pan
        pan_ymin = pan_ymax + pan_gt_clip[5] * rows_pan

        feedback.pushInfo(f"   PAN recortado: {cols_pan} x {rows_pan} px")

        # ---------------------------------------------------
        # 5. RECORTAR BANDAS MS (resolución original) + RESAMPLEAR PARA BROVEY
        # ---------------------------------------------------
        feedback.pushInfo("✂ Recortando bandas MS a resolución original...")

        # Arrays: ms_clips_paths → para exportar el MS nativo
        #         ms_resampled   → solo para el cálculo Brovey (a res PAN)
        ms_clip_paths = []
        ms_resampled  = np.zeros((4, rows_pan, cols_pan), dtype=np.float32)

        # Leer resolución nativa del primer MS para info
        ds_ms0     = gdal.Open(ms_paths[0])
        ms_res_nat = abs(ds_ms0.GetGeoTransform()[1])
        ds_ms0     = None
        feedback.pushInfo(f"   Resolución MS nativa: {ms_res_nat:.1f} m")

        for idx, ms_path in enumerate(ms_paths):

            ms_clip_path = os.path.join(temp_dir, f"ms_clip_B{idx+1}.tif")
            ms_res_path  = os.path.join(temp_dir, f"ms_resample_B{idx+1}.tif")

            # ── Recortar a resolución ORIGINAL del MS ──
            gdal.Warp(
                ms_clip_path,
                ms_path,
                cutlineDSName=aoi_shp,
                cutlineLayer="aoi_cut",
                cropToCutline=True,
                dstNodata=0,
                outputType=gdal.GDT_Float32,
                resampleAlg=gdal.GRA_Bilinear
            )
            ms_clip_paths.append(ms_clip_path)

            # ── Resamplear a PAN — solo para uso interno en Brovey ──
            gdal.Warp(
                ms_res_path,
                ms_clip_path,
                width=cols_pan,
                height=rows_pan,
                outputBounds=(pan_xmin, pan_ymin, pan_xmax, pan_ymax),
                resampleAlg=gdal.GRA_Bilinear
            )

            ds_ms = gdal.Open(ms_res_path)
            ms_resampled[idx] = ds_ms.GetRasterBand(1).ReadAsArray().astype(np.float32)
            ds_ms = None

            feedback.pushInfo(f"   ✔ MS B{idx+1} listo")

        # ---------------------------------------------------
        # 6. PANSHARPENING BROVEY
        #    PS_i = (MS_i / Σ MS) × PAN
        # ---------------------------------------------------
        feedback.pushInfo("🔀 Aplicando fusión Brovey...")

        sum_ms = np.sum(ms_resampled, axis=0)
        sum_ms[sum_ms == 0] = np.nan

        ps_arr = (ms_resampled / sum_ms) * pan_arr
        ps_arr = np.nan_to_num(ps_arr, nan=0.0).astype(np.float32)

        band_names = ["B1_Blue", "B2_Green", "B3_Red", "B4_Nir"]

        # ---------------------------------------------------
        # 7. EXPORTAR MS A RESOLUCIÓN ORIGINAL (merge multibanda)
        # ---------------------------------------------------
        feedback.pushInfo("💾 Exportando MS a resolución original...")

        # Leer dimensiones y geotransform del primer clip nativo
        ds_ref    = gdal.Open(ms_clip_paths[0])
        cols_ms   = ds_ref.RasterXSize
        rows_ms   = ds_ref.RasterYSize
        gt_ms     = ds_ref.GetGeoTransform()
        proj_ms   = ds_ref.GetProjection()
        ds_ref    = None

        driver    = gdal.GetDriverByName("GTiff")
        ds_out_ms = driver.Create(
            output_ms, cols_ms, rows_ms, 4, gdal.GDT_Float32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out_ms.SetGeoTransform(gt_ms)
        ds_out_ms.SetProjection(proj_ms)

        for i in range(4):
            ds_clip = gdal.Open(ms_clip_paths[i])
            arr     = ds_clip.GetRasterBand(1).ReadAsArray().astype(np.float32)
            ds_clip = None
            b = ds_out_ms.GetRasterBand(i + 1)
            b.WriteArray(arr)
            b.SetDescription(band_names[i])
            b.SetNoDataValue(0)

        ds_out_ms.FlushCache()
        ds_out_ms = None
        feedback.pushInfo(f"   MS exportado: {cols_ms} x {rows_ms} px @ {ms_res_nat:.1f} m")

        # ---------------------------------------------------
        # 8. EXPORTAR PANSHARPENING
        # ---------------------------------------------------
        feedback.pushInfo("💾 Exportando imagen pansharpenizada...")

        ds_out_ps = driver.Create(
            output_ps, cols_pan, rows_pan, 4, gdal.GDT_Float32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out_ps.SetGeoTransform(pan_gt_clip)
        ds_out_ps.SetProjection(pan_proj)

        for i in range(4):
            b = ds_out_ps.GetRasterBand(i + 1)
            b.WriteArray(ps_arr[i])
            b.SetDescription(band_names[i])
            b.SetNoDataValue(0)

        ds_out_ps.FlushCache()
        ds_out_ps = None

        # ---------------------------------------------------
        # 9. LIMPIEZA
        # ---------------------------------------------------
        feedback.pushInfo("🧹 Eliminando temporales...")
        gc.collect()

        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            feedback.pushInfo("✔ Temporales eliminados")
        except Exception:
            feedback.pushInfo("⚠ No se pudo eliminar temporales")

        feedback.pushInfo("✅ PROCESO CBERS-04A COMPLETO")

        return {
            self.OUTPUT_MS: output_ms,
            self.OUTPUT_PS: output_ps
        }

    def run(self):
        processing.execAlgorithmDialog(self)