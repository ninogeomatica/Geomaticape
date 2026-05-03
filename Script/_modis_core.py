# -*- coding: utf-8 -*-
"""
_modis_core.py — Núcleo compartido para herramientas MODIS
Geomaticape Plugin
Autor: GEOMATICA AMBIENTAL  |  Version: 1.2

Lee subdatasets HDF4 con gdal.Open(sds_path) directamente
(el sds_path viene de GetSubDatasets() y gdal lo abre sin problemas
aunque rioxarray falle con ese string en Windows).

Flujo:
  1. gdal.Open(hdf)  → GetSubDatasets()  → lista de (path_sds, desc)
  2. gdal.Open(path_sds)  → ReadAsArray() + GetGeoTransform() + GetProjection()
  3. Aplicar factor / offset  (numpy)
  4. Escribir GeoTIFF temporal en proyección original  (gdal CreateCopy)
  5. Reproyectar a EPSG:4326 con gdal.Warp
  6. Guardar en carpeta de salida
"""

import os
import gc
import tempfile
import numpy as np


# ------------------------------------------------------------------
def listar_sds(ruta_hdf, feedback):
    """
    Retorna lista de (path_sds, descripcion) usando gdal.
    Nunca lanza excepción — devuelve [] y loguea si falla.
    """
    try:
        from osgeo import gdal
        gdal.UseExceptions()
        ds = gdal.Open(ruta_hdf, gdal.GA_ReadOnly)
        if ds is None:
            feedback.reportError(
                f"  ⚠ gdal no pudo abrir: {ruta_hdf}\n"
                "     Verifica que el HDF4 no esté dañado o incompleto.", False
            )
            return []
        sds = ds.GetSubDatasets()   # [(path, desc), ...]
        ds = None
        return sds   # lista completa con path Y descripción
    except Exception as e:
        feedback.reportError(f"  ⚠ Error leyendo subdatasets: {e}", False)
        return []


# ------------------------------------------------------------------
def procesar_banda(
    sds_path,       # path completo del subdataset (gdal lo abre nativamente)
    ruta_tif_out,   # destino final GeoTIFF
    factor,         # escala multiplicativa (float)
    offset,         # offset aditivo post-factor (float)
    nodata_out,     # valor nodata en la salida
    dtype_out,      # 'float32' | 'uint8'
    resample_nn,    # True → vecino más cercano (categórico)
    feedback
):
    """
    Lee el subdataset con gdal, aplica factor+offset, reproyecta a
    EPSG:4326 con gdal.Warp y guarda el GeoTIFF final.
    Retorna True si tuvo éxito, False si falló.
    """
    try:
        from osgeo import gdal, osr
        gdal.UseExceptions()

        # ── 1. Abrir subdataset ────────────────────────────────
        sds_ds = gdal.Open(sds_path, gdal.GA_ReadOnly)
        if sds_ds is None:
            feedback.reportError(
                f"     ⚠ gdal no pudo abrir el subdataset:\n"
                f"       {sds_path}", False
            )
            return False

        banda   = sds_ds.GetRasterBand(1)
        arr     = banda.ReadAsArray().astype("float64")
        gt      = sds_ds.GetGeoTransform()
        proj    = sds_ds.GetProjection()
        nrows, ncols = arr.shape

        # Nodata original (puede ser None si no está definido en el HDF)
        nodata_src = banda.GetNoDataValue()
        sds_ds = None

        # ── 2. Aplicar factor + offset ─────────────────────────
        if nodata_src is not None:
            mascara_nodata = (arr == nodata_src)
        else:
            mascara_nodata = None

        if factor != 1.0 or offset != 0.0:
            arr = arr * factor + offset

        # Restaurar nodata en la salida
        if mascara_nodata is not None:
            arr[mascara_nodata] = nodata_out

        arr = arr.astype(dtype_out)

        # ── 3. Escribir GeoTIFF temporal en proyección original ─
        gdal_dtype = (
            gdal.GDT_Byte    if dtype_out == "uint8"
            else gdal.GDT_Float32
        )

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tif", prefix="modis_tmp_")
        os.close(tmp_fd)

        driver = gdal.GetDriverByName("GTiff")
        tmp_ds = driver.Create(tmp_path, ncols, nrows, 1, gdal_dtype)
        tmp_ds.SetGeoTransform(gt)
        tmp_ds.SetProjection(proj)
        b_out = tmp_ds.GetRasterBand(1)
        b_out.SetNoDataValue(float(nodata_out))
        b_out.WriteArray(arr)
        tmp_ds.FlushCache()
        tmp_ds = None
        del arr

        # ── 4. Reproyectar a EPSG:4326 con gdal.Warp ──────────
        resample_alg = (
            gdal.GRA_NearestNeighbour if resample_nn
            else gdal.GRA_Bilinear
        )

        warp_opts = gdal.WarpOptions(
            format          = "GTiff",
            dstSRS          = "EPSG:4326",
            dstNodata       = nodata_out,
            resampleAlg     = resample_alg,
            creationOptions = ["COMPRESS=DEFLATE", "TILED=YES",
                               "BIGTIFF=IF_SAFER"],
        )

        gdal.Warp(ruta_tif_out, tmp_path, options=warp_opts)

        # ── 5. Limpieza ────────────────────────────────────────
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        gc.collect()
        return True

    except Exception as e:
        feedback.reportError(f"     ⚠ Error en procesar_banda: {e}", False)
        try:
            if 'tmp_path' in dir() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        gc.collect()
        return False
