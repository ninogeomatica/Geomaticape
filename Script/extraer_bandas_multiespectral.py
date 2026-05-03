"""
Extraer bandas de imagenes multiespectrales
============================================
Abre cualquier imagen multiespectral (Landsat, Sentinel-2, CBERS-04A,
PlanetScope, RapidEye, ASTER, MODIS, etc.) y exporta las bandas
seleccionadas como rasters individuales con el nombre de la banda en el
archivo (ej. imagen_Red.tif, imagen_NIR.tif, imagen_SWIR1.tif).

La herramienta:
  * Detecta automaticamente la cantidad de bandas, el nombre de cada una
    (band.GetDescription), la resolucion y el CRS.
  * Si la banda no tiene nombre asignado y el sensor es reconocible por la
    cantidad y el nombre del archivo, intenta deducirlo (Landsat 4-5 TM,
    Landsat 7 ETM+, Landsat 8/9 OLI, Sentinel-2).
  * Permite elegir cualquier subconjunto de bandas (multiseleccion).
  * Conserva CRS, GeoTransform y NoData de la imagen original.
  * Los archivos de salida llevan un sufijo "_<NombreBanda>".

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.11
Grupo : Procesamiento
"""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterString,
    QgsProcessingException
)
from qgis import processing
from osgeo import gdal, osr


# Heuristicas para deducir nombres de banda si el raster no los trae
SENSOR_HINTS = {
    # (numero de bandas) -> {keyword en path: lista de nombres en orden}
    7: {
        "DEFAULT_LANDSAT8": [
            "Coastal_Aerosol", "Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2"
        ]
    },
    6: {
        "DEFAULT_LANDSAT57": [
            "Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2"
        ]
    },
    10: {
        "DEFAULT_SENTINEL2": [
            "Aerosol", "Blue", "Green", "Red",
            "Red_Edge1", "Red_Edge2", "Red_Edge3",
            "NIR", "SWIR1", "SWIR2"
        ]
    },
    13: {
        "DEFAULT_SENTINEL2_FULL": [
            "Aerosol", "Blue", "Green", "Red",
            "Red_Edge1", "Red_Edge2", "Red_Edge3",
            "NIR", "Narrow_NIR", "Water_Vapor",
            "Cirrus", "SWIR1", "SWIR2"
        ]
    },
    4: {
        "DEFAULT_PS_4B": ["Blue", "Green", "Red", "NIR"]
    }
}


def _adivinar_nombres(path, nbands):
    """Si el GetDescription esta vacio, intenta deducir nombres por sensor."""
    p = path.lower()
    if nbands == 7 and any(t in p for t in ("lc08", "lc09", "lc8", "lc9")):
        return SENSOR_HINTS[7]["DEFAULT_LANDSAT8"]
    if nbands == 6 and any(t in p for t in ("lt05", "lt04", "le07", "lt5", "le7")):
        return SENSOR_HINTS[6]["DEFAULT_LANDSAT57"]
    if nbands in (10, 13) and "s2" in p or "sentinel" in p or "msil" in p:
        return SENSOR_HINTS[nbands].get(
            "DEFAULT_SENTINEL2" if nbands == 10 else "DEFAULT_SENTINEL2_FULL"
        )
    if nbands == 4 and any(t in p for t in ("ps4b", "planetscope")):
        return SENSOR_HINTS[4]["DEFAULT_PS_4B"]
    return None


def _safe_filename(name):
    """Limpia el nombre para usarlo como sufijo de archivo."""
    nm = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(name))
    return nm.strip("_") or "Banda"


def _info_basica(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise QgsProcessingException(f"GDAL no pudo abrir {path}")
    info = {
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "nbands": ds.RasterCount,
        "gt": ds.GetGeoTransform(),
        "proj": ds.GetProjection(),
        "px": abs(ds.GetGeoTransform()[1]),
        "py": abs(ds.GetGeoTransform()[5]),
        "band_names": [],
        "nodata": [],
        "dtypes": [],
    }
    for i in range(1, ds.RasterCount + 1):
        b = ds.GetRasterBand(i)
        info["band_names"].append(b.GetDescription())
        info["nodata"].append(b.GetNoDataValue())
        info["dtypes"].append(b.DataType)
    ds = None

    # Deduccion si los nombres vienen vacios
    if not any(info["band_names"]):
        deducidos = _adivinar_nombres(path, info["nbands"])
        if deducidos:
            info["band_names"] = list(deducidos)
            info["band_names_source"] = "deducido"
        else:
            info["band_names"] = [f"Banda_{i}" for i in range(1, info["nbands"] + 1)]
            info["band_names_source"] = "auto"
    else:
        # Rellenar las que esten vacias
        info["band_names"] = [
            (n if n else f"Banda_{i}")
            for i, n in enumerate(info["band_names"], 1)
        ]
        info["band_names_source"] = "raster"
    return info


class ExtraerBandasMultiespectral(QgsProcessingAlgorithm):

    INPUT_RASTER = "INPUT_RASTER"
    BANDS = "BANDS"
    PREFIX = "PREFIX"
    OUT_FOLDER = "OUT_FOLDER"

    def __init__(self):
        super().__init__()
        # Llenamos la lista de bandas la primera vez que se conoce el raster
        self._opciones_cache = ["(carga la imagen para ver bandas)"]

    def name(self):
        return "extraer_bandas_multiespectral"

    def displayName(self):
        return "Extraer bandas de imagenes multiespectrales"

    def group(self):
        return "Procesamiento"

    def groupId(self):
        return "geomaticape_procesamiento"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "extraer_bandas.png"))

    def createInstance(self):
        return ExtraerBandasMultiespectral()

    def shortHelpString(self):
        return """
<h3>Extraer bandas de imagenes multiespectrales</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.11<br><br>

<b>Descripcion:</b><br>
Exporta cada banda seleccionada de un raster multiespectral como un
GeoTIFF independiente. La herramienta detecta automaticamente:
<ul>
<li>numero de bandas</li>
<li>nombre de cada banda (band.GetDescription, ej. Red, NIR, SWIR1)</li>
<li>resolucion espacial</li>
<li>sistema de coordenadas (CRS)</li>
</ul>

Si el raster no trae los nombres metados, los intenta deducir cuando el
sensor es reconocible (Landsat 5/7/8-9 OLI, Sentinel-2, PlanetScope 4B).
En caso contrario los etiqueta como Banda_1, Banda_2, etc.<br><br>

<b>Salida:</b> en la carpeta seleccionada, un GeoTIFF por banda con el
nombre <i>nombreImagen_NombreBanda.tif</i> (ej. <code>L8_2024_Red.tif</code>,
<code>L8_2024_NIR.tif</code>). Conserva CRS, GeoTransform y NoData del
original. Compresion LZW.
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_RASTER, "Imagen multiespectral"
        ))
        # Las opciones se generan dinamicamente al ejecutar; aqui usamos un
        # placeholder que QGIS tolera.
        self.addParameter(QgsProcessingParameterString(
            self.BANDS,
            "Bandas a extraer (numeros separados por coma; vacio = todas)",
            defaultValue="",
            optional=True
        ))
        self.addParameter(QgsProcessingParameterString(
            self.PREFIX,
            "Prefijo del nombre de archivo (vacio = nombre del raster)",
            defaultValue="",
            optional=True
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUT_FOLDER, "Carpeta de salida"
        ))

    def processAlgorithm(self, parameters, context, feedback):
        rl = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        bands_str = self.parameterAsString(parameters, self.BANDS, context).strip()
        prefix = self.parameterAsString(parameters, self.PREFIX, context).strip()
        out_dir = self.parameterAsString(parameters, self.OUT_FOLDER, context)

        if rl is None:
            raise QgsProcessingException("No se cargo el raster.")
        os.makedirs(out_dir, exist_ok=True)

        path = rl.source()
        info = _info_basica(path)

        # Bandas a procesar
        if bands_str:
            try:
                bandas = sorted({int(x.strip()) for x in bands_str.split(",")
                                 if x.strip()})
            except ValueError:
                raise QgsProcessingException(
                    "Lista de bandas no valida. Usa numeros separados por coma, "
                    "ej: 1,3,4"
                )
            for b in bandas:
                if b < 1 or b > info["nbands"]:
                    raise QgsProcessingException(
                        f"Banda {b} fuera de rango (la imagen tiene {info['nbands']})."
                    )
        else:
            bandas = list(range(1, info["nbands"] + 1))

        if not prefix:
            prefix = os.path.splitext(os.path.basename(path))[0]

        # Reportar info
        sref = osr.SpatialReference()
        if info["proj"]:
            sref.ImportFromWkt(info["proj"])
        authid = ""
        if sref.GetAuthorityCode(None):
            authid = f"{sref.GetAuthorityName(None)}:{sref.GetAuthorityCode(None)}"

        feedback.pushInfo("====================================================")
        feedback.pushInfo("Extraer bandas de imagen multiespectral")
        feedback.pushInfo(f"Raster   : {os.path.basename(path)}")
        feedback.pushInfo(f"Tamano   : {info['cols']} x {info['rows']} px")
        feedback.pushInfo(f"Bandas   : {info['nbands']} (fuente nombres: "
                          f"{info['band_names_source']})")
        feedback.pushInfo(f"Resol.   : {info['px']:.4f} x {info['py']:.4f}")
        feedback.pushInfo(f"CRS      : {authid}")
        feedback.pushInfo("Lista de bandas detectadas:")
        for i, nm in enumerate(info["band_names"], 1):
            marca = "*" if i in bandas else " "
            feedback.pushInfo(f"  {marca} Banda {i:2d}: {nm}  "
                              f"(NoData={info['nodata'][i-1]})")
        feedback.pushInfo("====================================================")

        # Escribir un info_raster.txt en la carpeta
        info_txt = os.path.join(out_dir, f"{prefix}_info_bandas.txt")
        with open(info_txt, "w", encoding="utf-8") as fh:
            fh.write("=== Imagen multiespectral ===\n")
            fh.write(f"Archivo  : {path}\n")
            fh.write(f"Tamano   : {info['cols']} x {info['rows']} px\n")
            fh.write(f"Bandas   : {info['nbands']}\n")
            fh.write(f"Pixel    : {info['px']:.4f} x {info['py']:.4f}\n")
            fh.write(f"CRS      : {authid}\n")
            fh.write(f"Fuente nombres bandas: {info['band_names_source']}\n")
            fh.write("\n=== Bandas ===\n")
            for i, nm in enumerate(info["band_names"], 1):
                fh.write(f"  Banda {i:2d}: {nm}  (NoData={info['nodata'][i-1]})\n")

        # Procesar banda a banda
        ds_in = gdal.Open(path, gdal.GA_ReadOnly)
        drv = gdal.GetDriverByName("GTiff")
        salidas = []
        nb_total = len(bandas)
        for k, b in enumerate(bandas, 1):
            band_name = info["band_names"][b - 1]
            safe = _safe_filename(band_name)
            ruta_out = os.path.join(out_dir, f"{prefix}_{safe}.tif")
            in_band = ds_in.GetRasterBand(b)
            arr = in_band.ReadAsArray()
            ds_out = drv.Create(
                ruta_out, info["cols"], info["rows"], 1, info["dtypes"][b - 1],
                options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
            )
            ds_out.SetGeoTransform(info["gt"])
            ds_out.SetProjection(info["proj"])
            ob = ds_out.GetRasterBand(1)
            ob.WriteArray(arr)
            ob.SetDescription(band_name)
            nd = info["nodata"][b - 1]
            if nd is not None:
                ob.SetNoDataValue(nd)
            ds_out.FlushCache()
            ds_out = None
            salidas.append(ruta_out)
            feedback.pushInfo(f"  -> {ruta_out}")
            feedback.setProgress(int(k * 100 / nb_total))
            if feedback.isCanceled():
                break
        ds_in = None

        feedback.pushInfo(f"OK - {len(salidas)} bandas extraidas en: {out_dir}")
        return {self.OUT_FOLDER: out_dir}

    def run(self):
        processing.execAlgorithmDialog(self)
