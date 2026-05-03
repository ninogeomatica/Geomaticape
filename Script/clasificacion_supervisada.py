"""
Clasificacion supervisada y validacion
=======================================
Para cualquier imagen multiespectral (Landsat MSS/TM/ETM+/OLI, Sentinel-2,
CBERS-04A, PlanetScope, etc.) entrena un clasificador supervisado a partir
de un shapefile de poligonos ROI (con campos ID y Clase) y genera:

  - roi_train.shp   (puntos: clase + valores de banda + x,y)
  - roi_test.shp    (puntos: clase + valores de banda + x,y)
  - clasificacion.tif  (raster clasificado, mismo CRS y resolucion)
  - matriz_confusion.csv y .png
  - metricas.txt    (Overall Accuracy, Kappa, classification_report)

Algoritmos disponibles (sklearn):
  - Decision Tree
  - Random Forest
  - Naive Bayes (Gaussiano)
  - Multi-Layer Perceptron (red neuronal)
  - K-Nearest Neighbors

La herramienta detecta automaticamente:
  * numero de bandas
  * nombre de cada banda (band.GetDescription)
  * resolucion del pixel
  * CRS / proyeccion
y reproyecta automaticamente el ROI al CRS del raster si es necesario.

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.10
Grupo : Geoprocesamiento
"""

import os
import csv
import gc

import numpy as np

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFolderDestination,
    QgsProcessingException,
    QgsProcessing,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsVectorLayer, QgsRasterLayer,
    QgsField, QgsFields, QgsFeature, QgsGeometry, QgsPointXY,
    QgsVectorFileWriter
)
from qgis import processing
from osgeo import gdal, ogr, osr


# Catalogo de algoritmos. Cada entrada produce el modelo cuando se llama
# con (seed). El parametro `seed` puede ignorarse en algunos.
def _build_models():
    models = {}
    try:
        from sklearn.tree import DecisionTreeClassifier
        models["Decision Tree"] = lambda s: DecisionTreeClassifier(random_state=s)
    except ImportError:
        pass
    try:
        from sklearn.ensemble import RandomForestClassifier
        models["Random Forest"] = lambda s: RandomForestClassifier(
            n_estimators=100, n_jobs=-1, random_state=s
        )
    except ImportError:
        pass
    try:
        from sklearn.naive_bayes import GaussianNB
        models["Naive Bayes"] = lambda s: GaussianNB()
    except ImportError:
        pass
    try:
        from sklearn.neural_network import MLPClassifier
        models["Red neuronal (MLP)"] = lambda s: MLPClassifier(
            hidden_layer_sizes=(64, 32), max_iter=500, random_state=s
        )
    except ImportError:
        pass
    try:
        from sklearn.neighbors import KNeighborsClassifier
        models["K-Nearest Neighbors"] = lambda s: KNeighborsClassifier(n_neighbors=5)
    except ImportError:
        pass
    return models


# =========================================================================

def _info_raster(path):
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
    }
    for i in range(1, ds.RasterCount + 1):
        b = ds.GetRasterBand(i)
        nm = b.GetDescription()
        info["band_names"].append(nm if nm else f"Banda_{i}")
        info["nodata"].append(b.GetNoDataValue())
    ds = None
    return info


def _safe_field(name, max_len=10):
    """Trunca el nombre a max_len y reemplaza caracteres conflictivos."""
    nm = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return nm[:max_len] if len(nm) > max_len else nm


def _rasterizar_roi(roi_path, info, campo_id, dst_path):
    """Rasteriza el ROI sobre la grilla del raster con valores = ID."""
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(dst_path, info["cols"], info["rows"], 1, gdal.GDT_Int32)
    out.SetGeoTransform(info["gt"])
    out.SetProjection(info["proj"])
    band = out.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.Fill(0)

    src = ogr.Open(roi_path)
    layer = src.GetLayer()
    gdal.RasterizeLayer(out, [1], layer, options=[f"ATTRIBUTE={campo_id}"])
    out.FlushCache()
    out = None
    src = None


def _escribir_shp_puntos(ruta, gt, proj, datos_filas, columnas, tipos):
    """
    datos_filas: lista de tuplas (x_geo, y_geo, [valores...])
    columnas: nombres de columnas de atributos (excluyendo geometria)
    tipos:    QVariant.* mismo orden que columnas
    """
    fields = QgsFields()
    for nm, t in zip(columnas, tipos):
        if t == QVariant.Double:
            fields.append(QgsField(nm, QVariant.Double, "double", 20, 6))
        elif t == QVariant.Int:
            fields.append(QgsField(nm, QVariant.Int))
        elif t == QVariant.LongLong:
            fields.append(QgsField(nm, QVariant.LongLong))
        else:
            fields.append(QgsField(nm, QVariant.String, len=80))

    crs_authid = ""
    sref = osr.SpatialReference()
    if proj:
        sref.ImportFromWkt(proj)
        if sref.GetAuthorityCode(None):
            crs_authid = f"{sref.GetAuthorityName(None)}:{sref.GetAuthorityCode(None)}"

    crs_str = crs_authid if crs_authid else "EPSG:4326"
    mem = QgsVectorLayer(f"Point?crs={crs_str}", "tmp_pts", "memory")
    pr = mem.dataProvider()
    pr.addAttributes(fields)
    mem.updateFields()
    feats = []
    for fila in datos_filas:
        x, y = fila[0], fila[1]
        attrs = list(fila[2:])
        f = QgsFeature(fields)
        f.setAttributes(attrs)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feats.append(f)
    pr.addFeatures(feats)
    mem.updateExtents()

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "ESRI Shapefile"
    opts.fileEncoding = "UTF-8"
    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem, ruta, QgsProject.instance().transformContext(), opts
    )
    if isinstance(err, tuple) and err[0] != QgsVectorFileWriter.NoError:
        raise QgsProcessingException(f"No se pudo escribir {ruta}: {err[1]}")


def _matriz_confusion(y_true, y_pred, clases):
    n = len(clases)
    idx = {c: i for i, c in enumerate(clases)}
    M = np.zeros((n, n), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        if t in idx and p in idx:
            M[idx[t], idx[p]] += 1
    return M


def _kappa(M):
    M = M.astype(np.float64)
    total = M.sum()
    if total <= 0:
        return float("nan")
    po = np.trace(M) / total
    pe = (M.sum(axis=0) * M.sum(axis=1)).sum() / (total * total)
    if (1 - pe) <= 0:
        return float("nan")
    return (po - pe) / (1 - pe)


def _plot_matriz(M, clases, ruta_png, oa, kappa):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return  # opcional
    fig, ax = plt.subplots(figsize=(1.2 * max(6, len(clases)),
                                    1.0 * max(5, len(clases))))
    im = ax.imshow(M, cmap="Blues")
    ax.set_xticks(range(len(clases)))
    ax.set_yticks(range(len(clases)))
    ax.set_xticklabels([str(c) for c in clases], rotation=45, ha="right")
    ax.set_yticklabels([str(c) for c in clases])
    ax.set_xlabel("Predicho")
    ax.set_ylabel("Real")
    ax.set_title(f"Matriz de confusion\nOA={oa:.4f}  Kappa={kappa:.4f}")
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            ax.text(j, i, str(int(M[i, j])),
                    ha="center", va="center",
                    color="white" if M[i, j] > M.max() / 2 else "black")
    fig.colorbar(im, ax=ax, fraction=0.045)
    fig.tight_layout()
    fig.savefig(ruta_png, dpi=120)
    plt.close(fig)


# =========================================================================

class ClasificacionSupervisada(QgsProcessingAlgorithm):

    INPUT_RASTER = "INPUT_RASTER"
    INPUT_ROI    = "INPUT_ROI"
    FIELD_ID     = "FIELD_ID"
    FIELD_CLASS  = "FIELD_CLASS"
    PCT_VAL      = "PCT_VAL"
    METHOD       = "METHOD"
    SEED         = "SEED"
    OUT_FOLDER   = "OUT_FOLDER"

    def __init__(self):
        super().__init__()
        self._models_dict = _build_models()
        self._method_keys = list(self._models_dict.keys())

    def name(self):
        return "clasificacion_supervisada"

    def displayName(self):
        return "Clasificacion supervisada y validacion"

    def group(self):
        return "Geoprocesamiento"

    def groupId(self):
        return "geomaticape_geoprocesamiento"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "clasif_supervisada.png"))

    def createInstance(self):
        return ClasificacionSupervisada()

    def shortHelpString(self):
        algos = ", ".join(self._method_keys) if self._method_keys else "(sklearn no instalado)"
        return f"""
<h3>Clasificacion supervisada y validacion</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.10<br><br>

<b>Descripcion:</b><br>
Entrena un clasificador supervisado sobre cualquier imagen multiespectral
(Landsat, Sentinel-2, CBERS-04A, PlanetScope, etc.) usando un shapefile
de poligonos ROI con dos campos: <i>ID</i> (entero 1,2,3...) y
<i>Clase</i> (Bosque, Pastizal, etc.). La herramienta detecta
automaticamente las bandas, nombres, resolucion y CRS del raster.<br><br>

<b>Algoritmos disponibles:</b> {algos}.<br><br>

<b>Salidas (en la carpeta seleccionada):</b>
<ul>
<li><b>roi_train.shp</b> y <b>roi_test.shp</b>: shapefile de puntos con
los valores extraidos de cada banda + ID + Clase + (x,y).</li>
<li><b>clasificacion.tif</b>: raster clasificado.</li>
<li><b>matriz_confusion.csv</b> y <b>matriz_confusion.png</b>.</li>
<li><b>metricas.txt</b>: Overall Accuracy, Kappa, reporte por clase.</li>
<li><b>info_raster.txt</b>: bandas, resolucion, CRS detectados.</li>
</ul>

<b>Validacion:</b> split estratificado por clase (mantiene proporcion).
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_RASTER, "Imagen multiespectral"
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_ROI, "Shapefile ROI (poligonos)",
            types=[QgsProcessing.TypeVectorPolygon]
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_ID, "Campo ID (entero, ej. 1,2,3)",
            parentLayerParameterName=self.INPUT_ROI,
            defaultValue="ID"
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CLASS, "Campo Clase (texto)",
            parentLayerParameterName=self.INPUT_ROI,
            defaultValue="Clase"
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.PCT_VAL, "Porcentaje de validacion (%)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=30, minValue=5, maxValue=80
        ))
        if not self._method_keys:
            self._method_keys = ["(scikit-learn no instalado)"]
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, "Algoritmo de clasificacion",
            options=self._method_keys,
            defaultValue=0, allowMultiple=False
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEED, "Semilla (random_state)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=42, minValue=0, maxValue=99999
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUT_FOLDER, "Carpeta de salida"
        ))

    def processAlgorithm(self, parameters, context, feedback):
        try:
            from sklearn.model_selection import train_test_split
            from sklearn.metrics import classification_report
        except ImportError:
            raise QgsProcessingException(
                "Falta scikit-learn. Instala con:\n"
                "  python -m pip install scikit-learn"
            )

        rl = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        vl = self.parameterAsVectorLayer(parameters, self.INPUT_ROI, context)
        f_id = self.parameterAsString(parameters, self.FIELD_ID, context)
        f_cls = self.parameterAsString(parameters, self.FIELD_CLASS, context)
        pct = self.parameterAsInt(parameters, self.PCT_VAL, context)
        m_idx = self.parameterAsEnum(parameters, self.METHOD, context)
        seed = self.parameterAsInt(parameters, self.SEED, context)
        out_dir = self.parameterAsString(parameters, self.OUT_FOLDER, context)

        if rl is None:
            raise QgsProcessingException("No se cargo el raster.")
        if vl is None:
            raise QgsProcessingException("No se cargo el ROI.")
        if not self._method_keys or self._method_keys[0].startswith("(scikit"):
            raise QgsProcessingException(
                "scikit-learn no esta instalado. Instala con pip y reinicia QGIS."
            )

        method_name = self._method_keys[m_idx]
        if method_name not in self._models_dict:
            raise QgsProcessingException(f"Metodo no disponible: {method_name}")
        model_factory = self._models_dict[method_name]

        os.makedirs(out_dir, exist_ok=True)
        raster_path = rl.source()
        info = _info_raster(raster_path)

        # Volcar info del raster
        info_txt = os.path.join(out_dir, "info_raster.txt")
        with open(info_txt, "w", encoding="utf-8") as fh:
            fh.write("=== Imagen multiespectral ===\n")
            fh.write(f"Archivo  : {raster_path}\n")
            fh.write(f"Tamano   : {info['cols']} x {info['rows']} px\n")
            fh.write(f"Bandas   : {info['nbands']}\n")
            fh.write(f"Pixel    : {info['px']:.4f} x {info['py']:.4f}\n")
            sref = osr.SpatialReference()
            if info['proj']:
                sref.ImportFromWkt(info['proj'])
            authid = ""
            if sref.GetAuthorityCode(None):
                authid = f"{sref.GetAuthorityName(None)}:{sref.GetAuthorityCode(None)}"
            fh.write(f"CRS      : {authid}\n")
            fh.write(f"GeoTransform: {info['gt']}\n")
            fh.write("\n=== Bandas ===\n")
            for i, nm in enumerate(info["band_names"], 1):
                fh.write(f"  Banda {i:2d}: {nm}  (NoData={info['nodata'][i-1]})\n")

        feedback.pushInfo("====================================================")
        feedback.pushInfo("Clasificacion supervisada y validacion")
        feedback.pushInfo(f"Raster   : {os.path.basename(raster_path)}")
        feedback.pushInfo(f"  bandas : {info['nbands']} -> {info['band_names']}")
        feedback.pushInfo(f"  pixel  : {info['px']} m, CRS WKT len={len(info['proj'])}")
        feedback.pushInfo(f"ROI      : {vl.name()} ({vl.featureCount()} poligonos)")
        feedback.pushInfo(f"Algoritmo: {method_name}, validacion={pct}%, seed={seed}")
        feedback.pushInfo("====================================================")

        # Reproyectar ROI al CRS del raster si no coincide
        crs_r = QgsCoordinateReferenceSystem()
        crs_r.createFromWkt(info["proj"])
        if not crs_r.isValid():
            crs_r = rl.crs()

        if vl.crs().authid() != crs_r.authid():
            feedback.pushInfo(
                f"Reproyectando ROI {vl.crs().authid()} -> {crs_r.authid()}"
            )
            res = processing.run(
                "native:reprojectlayer",
                {"INPUT": vl, "TARGET_CRS": crs_r, "OUTPUT": "memory:"},
                context=context, feedback=feedback
            )
            roi_layer = res["OUTPUT"]
        else:
            roi_layer = vl

        # Guardar ROI temporal a shapefile (gdal.RasterizeLayer necesita OGR)
        tmp_roi = os.path.join(out_dir, "_tmp_roi_proj.shp")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "ESRI Shapefile"
        opts.fileEncoding = "UTF-8"
        QgsVectorFileWriter.writeAsVectorFormatV3(
            roi_layer, tmp_roi, context.transformContext(), opts
        )

        # Mapeo ID -> Clase (a partir de los features)
        id2cls = {}
        for ft in roi_layer.getFeatures():
            try:
                ide = int(ft[f_id])
            except Exception:
                continue
            cls = ft[f_cls]
            if ide not in id2cls and cls not in (None, ""):
                id2cls[ide] = str(cls)
        if not id2cls:
            raise QgsProcessingException(
                f"No se encontraron pares ({f_id}, {f_cls}) validos."
            )

        # Rasterizar ROI (mascara con ID por pixel)
        rast_id = os.path.join(out_dir, "_tmp_roi_id.tif")
        _rasterizar_roi(tmp_roi, info, f_id, rast_id)

        # Leer mascara y bandas
        ds_mask = gdal.Open(rast_id, gdal.GA_ReadOnly)
        mask = ds_mask.GetRasterBand(1).ReadAsArray()
        ds_mask = None

        idx_train = mask > 0
        n_train_total = int(idx_train.sum())
        if n_train_total == 0:
            raise QgsProcessingException(
                "No se obtuvieron pixeles del ROI (revisa que los poligonos "
                "se solapen con el raster y los valores de ID sean enteros)."
            )
        feedback.pushInfo(f"Pixeles dentro del ROI: {n_train_total:,}")

        # Leer todas las bandas (fila plana solo de los pixeles ROI)
        ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
        Xrows = []
        for b in range(1, info["nbands"] + 1):
            arr = ds.GetRasterBand(b).ReadAsArray().astype(np.float64)
            nd = info["nodata"][b - 1]
            if nd is not None:
                arr[arr == nd] = np.nan
            Xrows.append(arr[idx_train])
        X_full = np.column_stack(Xrows)
        y_full = mask[idx_train].astype(np.int64)

        # Filtrar filas con NaN (algun NoData)
        ok = np.isfinite(X_full).all(axis=1)
        X_full = X_full[ok]
        y_full = y_full[ok]
        # tambien las coordenadas para guardar
        rows_idx, cols_idx = np.where(idx_train)
        rows_idx = rows_idx[ok]
        cols_idx = cols_idx[ok]
        gt = info["gt"]
        xgeo = gt[0] + (cols_idx + 0.5) * gt[1] + (rows_idx + 0.5) * gt[2]
        ygeo = gt[3] + (cols_idx + 0.5) * gt[4] + (rows_idx + 0.5) * gt[5]

        feedback.pushInfo(f"Muestras validas: {len(y_full):,}")
        feedback.setProgress(20)

        # Split estratificado
        try:
            X_tr, X_te, y_tr, y_te, xy_tr, xy_te = train_test_split(
                X_full, y_full, np.column_stack([xgeo, ygeo]),
                test_size=pct / 100.0, random_state=seed,
                stratify=y_full
            )
        except ValueError:
            # alguna clase con muy pocas muestras -> sin stratify
            X_tr, X_te, y_tr, y_te, xy_tr, xy_te = train_test_split(
                X_full, y_full, np.column_stack([xgeo, ygeo]),
                test_size=pct / 100.0, random_state=seed
            )
        feedback.pushInfo(f"Train: {len(y_tr):,}  Test: {len(y_te):,}")

        # Escribir shapefiles de muestras
        band_field_names = [_safe_field(nm) for nm in info["band_names"]]
        # asegurar unicidad
        seen = set(); out_names = []
        for nm in band_field_names:
            base = nm; k = 1
            while nm in seen:
                nm = (base[:8] + f"_{k}")[:10]; k += 1
            seen.add(nm); out_names.append(nm)
        band_field_names = out_names

        cols_attr = ["ID", "Clase"] + band_field_names
        tipos_attr = [QVariant.Int, QVariant.String] + [QVariant.Double] * info["nbands"]

        def _filas(X, y, XY):
            for i in range(len(y)):
                ide = int(y[i])
                cls = id2cls.get(ide, str(ide))
                yield (float(XY[i, 0]), float(XY[i, 1]),
                       ide, cls, *[float(v) for v in X[i]])

        train_shp = os.path.join(out_dir, "roi_train.shp")
        test_shp = os.path.join(out_dir, "roi_test.shp")
        _escribir_shp_puntos(train_shp, gt, info["proj"],
                             list(_filas(X_tr, y_tr, xy_tr)),
                             cols_attr, tipos_attr)
        _escribir_shp_puntos(test_shp, gt, info["proj"],
                             list(_filas(X_te, y_te, xy_te)),
                             cols_attr, tipos_attr)
        feedback.pushInfo(f"Escrito  : {train_shp}")
        feedback.pushInfo(f"Escrito  : {test_shp}")
        feedback.setProgress(40)

        # Entrenar
        feedback.pushInfo(f"Entrenando {method_name} ...")
        model = model_factory(seed)
        model.fit(X_tr, y_tr)
        feedback.setProgress(55)

        # Validar sobre test
        y_pred = model.predict(X_te)
        clases_id = sorted(np.unique(np.concatenate([y_te, y_pred])).tolist())
        clases_lbl = [f"{c} - {id2cls.get(int(c), str(c))}" for c in clases_id]
        M = _matriz_confusion(y_te, y_pred, clases_id)
        oa = float((y_pred == y_te).mean())
        kappa = float(_kappa(M))

        # CSV matriz
        ruta_csv = os.path.join(out_dir, "matriz_confusion.csv")
        with open(ruta_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([""] + clases_lbl)
            for i, lbl in enumerate(clases_lbl):
                w.writerow([lbl] + [int(v) for v in M[i]])

        # PNG matriz
        ruta_png = os.path.join(out_dir, "matriz_confusion.png")
        _plot_matriz(M, clases_lbl, ruta_png, oa, kappa)

        # Metricas
        report = classification_report(y_te, y_pred, zero_division=0)
        ruta_metr = os.path.join(out_dir, "metricas.txt")
        with open(ruta_metr, "w", encoding="utf-8") as fh:
            fh.write(f"Algoritmo : {method_name}\n")
            fh.write(f"Semilla   : {seed}\n")
            fh.write(f"Validacion: {pct}%\n")
            fh.write(f"Train n   : {len(y_tr)}\n")
            fh.write(f"Test  n   : {len(y_te)}\n")
            fh.write(f"\nOverall Accuracy: {oa:.6f}\n")
            fh.write(f"Kappa            : {kappa:.6f}\n")
            fh.write(f"\nMatriz de confusion (filas=real, columnas=predicho):\n")
            fh.write(",".join([""] + clases_lbl) + "\n")
            for i, lbl in enumerate(clases_lbl):
                fh.write(",".join([lbl] + [str(int(v)) for v in M[i]]) + "\n")
            fh.write("\nClassification report:\n")
            fh.write(report)

        feedback.pushInfo(f"Overall Accuracy = {oa:.4f}")
        feedback.pushInfo(f"Kappa            = {kappa:.4f}")
        feedback.pushInfo(f"Matriz CSV       : {ruta_csv}")
        feedback.pushInfo(f"Matriz PNG       : {ruta_png}")
        feedback.pushInfo(f"Metricas        : {ruta_metr}")
        feedback.setProgress(70)

        # Predecir todo el raster (en memoria) -> escribir clasificacion
        feedback.pushInfo("Prediciendo sobre todo el raster ...")
        all_bands = []
        for b in range(1, info["nbands"] + 1):
            arr = ds.GetRasterBand(b).ReadAsArray().astype(np.float64)
            nd = info["nodata"][b - 1]
            if nd is not None:
                arr[arr == nd] = np.nan
            all_bands.append(arr)
        ds = None
        stack = np.stack(all_bands, axis=-1)  # (rows, cols, nbands)
        h, w, nb = stack.shape
        flat = stack.reshape(-1, nb)
        ok_all = np.isfinite(flat).all(axis=1)
        pred_all = np.zeros(flat.shape[0], dtype=np.int32)  # 0 = sin clasificar
        if ok_all.any():
            pred_valid = model.predict(flat[ok_all]).astype(np.int32)
            pred_all[ok_all] = pred_valid
        out_arr = pred_all.reshape(h, w)
        del flat, stack, all_bands, pred_all, ok_all
        gc.collect()

        ruta_clas = os.path.join(out_dir, "clasificacion.tif")
        drv = gdal.GetDriverByName("GTiff")
        ds_out = drv.Create(
            ruta_clas, w, h, 1, gdal.GDT_Int32,
            options=["COMPRESS=LZW", "TILED=YES"]
        )
        ds_out.SetGeoTransform(info["gt"])
        ds_out.SetProjection(info["proj"])
        bo = ds_out.GetRasterBand(1)
        bo.WriteArray(out_arr)
        bo.SetNoDataValue(0)
        bo.SetDescription("Clase ID")
        ds_out.FlushCache()
        ds_out = None

        # Limpieza de temporales
        for tmp in (rast_id, tmp_roi):
            for ext in ("", ".dbf", ".shx", ".prj", ".cpg", ".qpj"):
                try:
                    p = tmp if ext == "" else tmp.replace(".shp", ext)
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

        feedback.pushInfo(f"Raster clasificado: {ruta_clas}")
        feedback.setProgress(100)
        feedback.pushInfo("Proceso COMPLETADO.")

        return {self.OUT_FOLDER: out_dir}

    def run(self):
        processing.execAlgorithmDialog(self)
