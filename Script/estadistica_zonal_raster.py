"""
Estadistica zonal de raster (mono o multiespectral)
====================================================
Calcula estadisticas zonales (count, min, max, mean, median, sum, std,
range, majority, minority) de los pixeles de un raster que caen dentro
de cada poligono. Si el raster es multiespectral, el usuario indica que
banda analizar. Las estadisticas se pueden seleccionar (todas por defecto).
Resultado: tabla en Excel (.xlsx) o CSV.

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.8
Grupo : Geoprocesamiento
"""

import os
import csv
import math
import tempfile

import numpy as np

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterBand,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsProcessingException,
    QgsProcessing,
    QgsCoordinateTransform,
    QgsProject,
    QgsVectorLayer,
    QgsRasterLayer
)
from qgis import processing
from osgeo import gdal, ogr, osr


# Catalogo de estadisticas soportadas (orden importa: aparece en la GUI)
STATS_LIST = [
    "count", "min", "max", "mean", "median",
    "sum", "std", "range", "majority", "minority"
]


def _calc_stats(arr_valid, stats_seleccionadas):
    """
    Devuelve dict {stat: valor} sobre arr_valid (1-D, sin NaN/NoData).
    """
    out = {}
    if arr_valid.size == 0:
        for s in stats_seleccionadas:
            out[s] = None
        return out

    for s in stats_seleccionadas:
        if s == "count":
            out[s] = int(arr_valid.size)
        elif s == "min":
            out[s] = float(np.min(arr_valid))
        elif s == "max":
            out[s] = float(np.max(arr_valid))
        elif s == "mean":
            out[s] = float(np.mean(arr_valid))
        elif s == "median":
            out[s] = float(np.median(arr_valid))
        elif s == "sum":
            out[s] = float(np.sum(arr_valid))
        elif s == "std":
            out[s] = float(np.std(arr_valid))
        elif s == "range":
            out[s] = float(np.max(arr_valid) - np.min(arr_valid))
        elif s in ("majority", "minority"):
            # discretizar a int para conteo de frecuencias
            try:
                vals_int = np.round(arr_valid).astype(np.int64)
                uniq, cnt = np.unique(vals_int, return_counts=True)
                if uniq.size == 0:
                    out[s] = None
                elif s == "majority":
                    out[s] = int(uniq[np.argmax(cnt)])
                else:
                    out[s] = int(uniq[np.argmin(cnt)])
            except Exception:
                out[s] = None
    return out


class EstadisticaZonalRaster(QgsProcessingAlgorithm):

    INPUT_VECTOR = "INPUT_VECTOR"
    INPUT_RASTER = "INPUT_RASTER"
    BAND         = "BAND"
    ID_FIELD     = "ID_FIELD"
    STATS        = "STATS"
    OUT_FORMAT   = "OUT_FORMAT"
    OUTPUT       = "OUTPUT"

    def name(self):
        return "estadistica_zonal_raster"

    def displayName(self):
        return "Estadistica zonal raster (Excel/CSV)"

    def group(self):
        return "Geoprocesamiento"

    def groupId(self):
        return "geomaticape_geoprocesamiento"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "zonal_raster.png"))

    def createInstance(self):
        return EstadisticaZonalRaster()

    def shortHelpString(self):
        return """
<h3>Estadistica zonal raster</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.8<br><br>

<b>Descripcion:</b><br>
Calcula estadisticas de los pixeles del raster que caen dentro de cada
poligono. Soporta raster mono o multiespectral (eliges la banda).
Las estadisticas se pueden seleccionar; por defecto se calculan todas:
<ul>
<li><b>count</b>   - numero de pixeles</li>
<li><b>min/max</b> - valores extremos</li>
<li><b>mean</b>    - promedio</li>
<li><b>median</b>  - mediana</li>
<li><b>sum</b>     - suma total</li>
<li><b>std</b>     - desviacion estandar</li>
<li><b>range</b>   - max - min</li>
<li><b>majority</b>- valor mas frecuente (entero)</li>
<li><b>minority</b>- menos frecuente (entero)</li>
</ul>

<b>Salida:</b> tabla Excel (.xlsx) o CSV con una fila por poligono.<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_VECTOR,
            "Capa de poligonos",
            types=[QgsProcessing.TypeVectorPolygon]
        ))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_RASTER,
            "Capa raster (mono o multiespectral)"
        ))
        self.addParameter(QgsProcessingParameterBand(
            self.BAND,
            "Banda a analizar (si es multiespectral)",
            parentLayerParameterName=self.INPUT_RASTER,
            defaultValue=1,
            optional=False
        ))
        self.addParameter(QgsProcessingParameterField(
            self.ID_FIELD,
            "Campo identificador del poligono (opcional)",
            parentLayerParameterName=self.INPUT_VECTOR,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.STATS,
            "Estadisticas a calcular (todas por defecto)",
            options=STATS_LIST,
            allowMultiple=True,
            defaultValue=list(range(len(STATS_LIST)))
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.OUT_FORMAT,
            "Formato de salida",
            options=["Excel (.xlsx)", "CSV (.csv)"],
            defaultValue=0,
            allowMultiple=False
        ))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT,
            "Tabla de resultados",
            fileFilter="Excel (*.xlsx);;CSV (*.csv)"
        ))

    def processAlgorithm(self, parameters, context, feedback):
        v_layer = self.parameterAsVectorLayer(parameters, self.INPUT_VECTOR, context)
        r_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        band_n  = self.parameterAsInt(parameters, self.BAND, context)
        id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
        stats_idx = self.parameterAsEnums(parameters, self.STATS, context)
        out_fmt = self.parameterAsEnum(parameters, self.OUT_FORMAT, context)
        out_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        if v_layer is None:
            raise QgsProcessingException("No se pudo cargar la capa de poligonos.")
        if r_layer is None:
            raise QgsProcessingException("No se pudo cargar la capa raster.")

        if not stats_idx:
            stats_seleccionadas = STATS_LIST[:]   # si no marca nada, todas
        else:
            stats_seleccionadas = [STATS_LIST[i] for i in stats_idx]

        # Asegurar extension de salida coherente con formato
        if out_fmt == 0 and not out_path.lower().endswith(".xlsx"):
            out_path = os.path.splitext(out_path)[0] + ".xlsx"
        elif out_fmt == 1 and not out_path.lower().endswith(".csv"):
            out_path = os.path.splitext(out_path)[0] + ".csv"

        feedback.pushInfo("====================================================")
        feedback.pushInfo("Estadistica zonal raster")
        feedback.pushInfo(f"Vector  : {v_layer.name()} ({v_layer.featureCount()} poligonos)")
        feedback.pushInfo(f"Raster  : {r_layer.name()} (banda {band_n})")
        feedback.pushInfo(f"Stats   : {', '.join(stats_seleccionadas)}")
        feedback.pushInfo(f"Salida  : {out_path}")
        feedback.pushInfo("====================================================")

        raster_path = r_layer.source()
        ds_r = gdal.Open(raster_path, gdal.GA_ReadOnly)
        if ds_r is None:
            raise QgsProcessingException("GDAL no pudo abrir el raster.")
        if band_n < 1 or band_n > ds_r.RasterCount:
            raise QgsProcessingException(
                f"Banda {band_n} fuera de rango (raster tiene {ds_r.RasterCount})."
            )
        gt = ds_r.GetGeoTransform()
        proj = ds_r.GetProjection()
        rcols = ds_r.RasterXSize
        rrows = ds_r.RasterYSize
        band = ds_r.GetRasterBand(band_n)
        nodata = band.GetNoDataValue()

        # CRS de raster vs vector -> reproyectar el vector si no coincide
        crs_vec = v_layer.crs()
        srs_r = osr.SpatialReference()
        if proj:
            srs_r.ImportFromWkt(proj)
        crs_r_authid = None
        if srs_r.GetAuthorityCode(None):
            crs_r_authid = f"{srs_r.GetAuthorityName(None)}:{srs_r.GetAuthorityCode(None)}"

        vector_to_use = v_layer
        if crs_r_authid and crs_vec.authid() != crs_r_authid:
            feedback.pushInfo(
                f"Reproyectando vector {crs_vec.authid()} -> {crs_r_authid}"
            )
            res = processing.run(
                "native:reprojectlayer",
                {
                    "INPUT": v_layer,
                    "TARGET_CRS": crs_r_authid,
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )
            vector_to_use = res["OUTPUT"]

        # Iterar sobre cada feature del vector
        feats = list(vector_to_use.getFeatures())
        total = len(feats)
        registros = []

        # field id real (puede no existir)
        fname = id_field if id_field else ""

        for i, feat in enumerate(feats):
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            bbox = geom.boundingBox()  # en CRS del raster

            # convertir bbox a indices del raster
            inv_gt = gdal.InvGeoTransform(gt)
            if inv_gt is None:
                raise QgsProcessingException("GeoTransform invalido del raster.")
            px_min, py_min = gdal.ApplyGeoTransform(inv_gt, bbox.xMinimum(), bbox.yMaximum())
            px_max, py_max = gdal.ApplyGeoTransform(inv_gt, bbox.xMaximum(), bbox.yMinimum())

            xoff = max(0, int(math.floor(min(px_min, px_max))))
            yoff = max(0, int(math.floor(min(py_min, py_max))))
            xend = min(rcols, int(math.ceil(max(px_min, px_max))))
            yend = min(rrows, int(math.ceil(max(py_min, py_max))))
            xsize = xend - xoff
            ysize = yend - yoff
            if xsize <= 0 or ysize <= 0:
                continue

            # subextent geografico alineado al raster
            sub_minx = gt[0] + xoff * gt[1] + yoff * gt[2]
            sub_maxy = gt[3] + xoff * gt[4] + yoff * gt[5]
            sub_gt = (sub_minx, gt[1], gt[2], sub_maxy, gt[4], gt[5])

            # leer subarray del raster
            arr = band.ReadAsArray(xoff, yoff, xsize, ysize)
            if arr is None:
                continue
            arr = arr.astype(np.float64)

            # --- crear mascara por rasterizacion en memoria ---
            drv = ogr.GetDriverByName("Memory")
            ds_v = drv.CreateDataSource("mem")
            srs = osr.SpatialReference()
            if proj:
                srs.ImportFromWkt(proj)
            lyr = ds_v.CreateLayer("p", srs=srs, geom_type=ogr.wkbPolygon)
            f = ogr.Feature(lyr.GetLayerDefn())
            wkb = bytes(geom.asWkb())
            ogr_geom = ogr.CreateGeometryFromWkb(wkb)
            f.SetGeometry(ogr_geom)
            lyr.CreateFeature(f)
            f = None

            mask_drv = gdal.GetDriverByName("MEM")
            mask_ds = mask_drv.Create("", xsize, ysize, 1, gdal.GDT_Byte)
            mask_ds.SetGeoTransform(sub_gt)
            if proj:
                mask_ds.SetProjection(proj)
            gdal.RasterizeLayer(mask_ds, [1], lyr, burn_values=[1])
            mask = mask_ds.GetRasterBand(1).ReadAsArray()
            mask_ds = None
            ds_v = None

            # aplicar mascara y nodata
            valid_mask = (mask == 1)
            if nodata is not None:
                valid_mask &= (arr != nodata)
            valid_mask &= np.isfinite(arr)

            arr_valid = arr[valid_mask]

            stat_vals = _calc_stats(arr_valid, stats_seleccionadas)

            # construir registro
            row = {}
            row["fid"] = feat.id()
            if fname:
                try:
                    row[fname] = feat[fname]
                except Exception:
                    row[fname] = None
            for s in stats_seleccionadas:
                row[s] = stat_vals.get(s)
            registros.append(row)

            feedback.setProgress(int((i + 1) * 100 / max(total, 1)))
            if feedback.isCanceled():
                break

        ds_r = None

        # --- escribir salida ---
        if not registros:
            raise QgsProcessingException("No se generaron registros (revisa CRS y extension).")

        cols = list(registros[0].keys())
        # asegurar mismo orden de columnas en todas las filas
        for r in registros:
            for c in cols:
                r.setdefault(c, None)

        if out_fmt == 0:
            self._escribir_excel(out_path, registros, cols)
        else:
            self._escribir_csv(out_path, registros, cols)

        feedback.pushInfo(f"OK - {len(registros)} registros escritos.")
        return {self.OUTPUT: out_path}

    # -----------------------------------------------------------
    def _escribir_excel(self, ruta, registros, cols):
        try:
            from openpyxl import Workbook
        except ImportError:
            raise QgsProcessingException(
                "Para exportar a Excel necesitas openpyxl:\n"
                "  python -m pip install openpyxl"
            )
        wb = Workbook()
        ws = wb.active
        ws.title = "zonal_stats"
        ws.append(cols)
        for r in registros:
            ws.append([r.get(c) for c in cols])
        wb.save(ruta)

    def _escribir_csv(self, ruta, registros, cols):
        with open(ruta, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in registros:
                w.writerow(r)

    def run(self):
        processing.execAlgorithmDialog(self)
