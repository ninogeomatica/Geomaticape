"""
Recortar raster(s) por zona de estudio
=======================================
Recorta uno o varios raster a una zona de estudio definida por:
  * una capa vectorial poligonal (cutline / mascara), o
  * una extension (xmin, ymin, xmax, ymax) en el CRS deseado.

Por cada raster se puede elegir:
  * usar TODAS sus bandas (multibanda original), o
  * extraer SOLO una banda especifica (1..N).

Tres modos de salida:
  1. Un archivo por entrada    : por cada fila de la tabla genera un
     GeoTIFF independiente (multibanda si se pide "Todas").
  2. Un archivo por banda      : si una entrada esta en modo "Todas"
     se separa en un archivo por banda (con sufijo del nombre).
  3. Stack multibanda combinado: todas las (raster, banda) seleccionadas
     se apilan en un solo GeoTIFF multibanda con nombres preservados.

UI Qt personalizada con tabla 4 columnas (#, Origen, Banda, Nombre),
picker para capas raster QGIS abiertas y selector de zona de estudio
(combo entre 'Por geometria (cutline)' y 'Por extension (bbox)').

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.18
Grupo : Procesamiento
"""

import os
import re
import tempfile

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox,
    QComboBox, QCheckBox, QDialogButtonBox, QProgressDialog,
    QApplication, QWidget, QAbstractItemView, QDoubleSpinBox,
    QListWidget, QListWidgetItem,
)
from osgeo import gdal, ogr

# Reusamos utilidades del modulo de Combinar bandas (mismo paquete).
from .combinar_bandas_nombres import (
    _safe_name,
    _default_name,
    _band_count,
    _detect_band_name_at,
    _DialogFeedback,
    _QGISLayerPickerDialog,
    COMPRESS_OPTIONS,
)


OUTPUT_MODE_OPTIONS = [
    "Un archivo por entrada (multibanda si banda='Todas')",
    "Un archivo por banda (separa cada banda)",
    "Stack multibanda combinado (un solo GeoTIFF con todas las bandas)",
]

CUTLINE_RESAMPLE = ["nearest", "bilinear", "cubic", "cubicspline",
                    "lanczos", "average", "mode"]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _open_info(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL no pudo abrir: {path}")
    info = {
        "path":   path,
        "cols":   ds.RasterXSize,
        "rows":   ds.RasterYSize,
        "gt":     ds.GetGeoTransform(),
        "proj":   ds.GetProjection(),
        "nbands": ds.RasterCount,
    }
    ds = None
    return info


def _read_band_descriptions(path, bands=None):
    out = {}
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        return out
    nb = ds.RasterCount
    if bands is None:
        bands = range(1, nb + 1)
    for b in bands:
        if 1 <= b <= nb:
            band = ds.GetRasterBand(b)
            out[b] = (band.GetDescription() or "").strip()
    ds = None
    return out


def _resolve_vector_source(qgs_layer):
    """Devuelve (file_path, layer_name_or_None) para un QgsVectorLayer.

    Si la capa es 'memory' la exporta a un GeoPackage temporal.
    """
    src = qgs_layer.source() if qgs_layer is not None else ""
    provider = ""
    try:
        provider = qgs_layer.providerType() if qgs_layer is not None else ""
    except Exception:
        provider = ""

    # Capa en memoria -> exportar a GPKG temporal
    if provider == "memory" or not src or src.startswith("memory"):
        try:
            from qgis.core import QgsVectorFileWriter, QgsCoordinateTransformContext
            tmp = os.path.join(
                tempfile.gettempdir(),
                f"_geomaticape_cutline_{os.getpid()}.gpkg"
            )
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"
            opts.fileEncoding = "UTF-8"
            opts.layerName = _safe_name(qgs_layer.name())
            try:
                QgsVectorFileWriter.writeAsVectorFormatV3(
                    qgs_layer, tmp,
                    QgsCoordinateTransformContext(),
                    opts,
                )
            except Exception:
                # Fallback API antigua
                QgsVectorFileWriter.writeAsVectorFormat(
                    qgs_layer, tmp, "UTF-8",
                    qgs_layer.crs(),
                    "GPKG"
                )
            return tmp, opts.layerName
        except Exception as e:
            raise RuntimeError(
                f"No se pudo exportar la capa en memoria a GPKG: {e}"
            )

    # Algunas capas tienen URI tipo "ruta.gpkg|layername=foo|geometrytype=..."
    if "|" in src:
        parts = src.split("|")
        path = parts[0]
        lname = None
        for p in parts[1:]:
            if p.startswith("layername="):
                lname = p.split("=", 1)[1]
        return path, lname

    return src, None


def _vector_extent_in_srs(vector_path, layer_name=None):
    """Devuelve (xmin, ymin, xmax, ymax, srs_wkt) del cutline."""
    ds = ogr.Open(vector_path)
    if ds is None:
        raise RuntimeError(f"OGR no pudo abrir: {vector_path}")
    if layer_name:
        layer = ds.GetLayerByName(layer_name)
        if layer is None:
            layer = ds.GetLayer(0)
    else:
        layer = ds.GetLayer(0)
    if layer is None:
        ds = None
        raise RuntimeError(f"No se pudo leer ninguna capa de: {vector_path}")
    extent = layer.GetExtent()  # (minx, maxx, miny, maxy)
    sref = layer.GetSpatialRef()
    wkt = sref.ExportToWkt() if sref else ""
    ds = None
    return (extent[0], extent[2], extent[1], extent[3], wkt)


# ---------------------------------------------------------------------------
# Logica de procesamiento
# ---------------------------------------------------------------------------

def _warp_one(src_path, src_band, out_path,
              cutline_path, cutline_layer, crop_to_cutline,
              bbox, bbox_srs,
              dst_srs, resample, compress, feedback):
    """Aplica gdal.Warp para recortar src_path -> out_path.

    src_band: 0 = todas las bandas; >0 = solo esa banda.
    """
    creation = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if compress and compress != "NONE":
        creation.append(f"COMPRESS={compress}")

    warp_kwargs = dict(
        format="GTiff",
        multithread=True,
        resampleAlg=resample,
        creationOptions=creation,
    )

    if src_band and src_band > 0:
        # gdal.Warp acepta srcBands en builds modernas; si falla,
        # caemos a Translate primero.
        warp_kwargs["srcBands"] = [int(src_band)]

    if cutline_path:
        warp_kwargs["cutlineDSName"] = cutline_path
        if cutline_layer:
            warp_kwargs["cutlineLayer"] = cutline_layer
        warp_kwargs["cropToCutline"] = bool(crop_to_cutline)
    elif bbox is not None:
        warp_kwargs["outputBounds"] = tuple(bbox)
        if bbox_srs:
            warp_kwargs["outputBoundsSRS"] = bbox_srs

    if dst_srs:
        warp_kwargs["dstSRS"] = dst_srs

    try:
        gdal.Warp(out_path, src_path, **warp_kwargs)
    except TypeError:
        # Esta build no soporta srcBands en Warp -> Translate primero.
        if src_band and src_band > 0:
            tmp_b = out_path + ".bandsel.vrt"
            gdal.Translate(tmp_b, src_path, format="VRT", bandList=[int(src_band)])
            warp_kwargs.pop("srcBands", None)
            try:
                gdal.Warp(out_path, tmp_b, **warp_kwargs)
            finally:
                try:
                    os.remove(tmp_b)
                except OSError:
                    pass
        else:
            raise
    return out_path


def _apply_band_descriptions(out_path, names):
    """Escribe band.SetDescription(...) en cada banda del out_path."""
    ds = gdal.Open(out_path, gdal.GA_Update)
    if ds is None:
        return
    nb = ds.RasterCount
    for i, nm in enumerate(names[:nb], start=1):
        b = ds.GetRasterBand(i)
        if nm:
            b.SetDescription(nm)
            try:
                b.SetMetadataItem("BAND_NAME", nm)
            except Exception:
                pass
    ds.FlushCache()
    ds = None


def ejecutar_recorte(rows, mode_output, out_dir, out_stack_path,
                     cutline_path, cutline_layer, crop_to_cutline,
                     bbox, bbox_srs, dst_srs, resample, compress,
                     feedback):
    """Ejecuta el recorte segun configuracion."""
    if not rows:
        raise RuntimeError("Agrega al menos un raster a la tabla.")

    is_cutline = bool(cutline_path)
    is_bbox = (bbox is not None)
    if not is_cutline and not is_bbox:
        raise RuntimeError(
            "Define la zona de estudio: una capa vectorial (cutline) o un bbox."
        )

    # Resumen.
    feedback.pushInfo("=" * 64)
    feedback.pushInfo("Recortar raster(s) por zona de estudio")
    feedback.pushInfo(f"Modo zona: "
                      f"{'cutline (vector)' if is_cutline else 'extension (bbox)'}")
    if is_cutline:
        feedback.pushInfo(f"Cutline   : {cutline_path}"
                          + (f"  layer={cutline_layer}" if cutline_layer else "")
                          + f"  cropToCutline={crop_to_cutline}")
    else:
        feedback.pushInfo(
            f"BBox      : xmin={bbox[0]} ymin={bbox[1]} "
            f"xmax={bbox[2]} ymax={bbox[3]}  srs={bbox_srs or '(de la fuente)'}"
        )
    if dst_srs:
        feedback.pushInfo(f"CRS salida: {dst_srs}")
    feedback.pushInfo(f"Remuestreo: {resample}")
    feedback.pushInfo(f"Compresion: {compress}")
    feedback.pushInfo(f"Modo salida: {OUTPUT_MODE_OPTIONS[mode_output]}")
    feedback.pushInfo(f"Entradas: {len(rows)}")
    for i, r in enumerate(rows, 1):
        b = r["band_idx"]
        feedback.pushInfo(
            f"  [{i}] {os.path.basename(r['source_path'])}  "
            f"banda={'Todas' if b == 0 else b}  nombre='{r['name']}'  "
            f"(nbands fuente={r['nbands']})"
        )
    feedback.pushInfo("=" * 64)

    # Validar carpeta de salida
    if mode_output in (0, 1):
        if not out_dir:
            raise RuntimeError(
                "Define la carpeta de salida para el modo seleccionado."
            )
        os.makedirs(out_dir, exist_ok=True)
    if mode_output == 2:
        if not out_stack_path:
            raise RuntimeError(
                "Define la ruta del GeoTIFF de stack multibanda de salida."
            )
        os.makedirs(os.path.dirname(out_stack_path) or ".", exist_ok=True)

    salidas = []
    tmp_files = []
    n = len(rows)

    if mode_output == 0:
        # MODO 0: un archivo por entrada (multibanda si Todas).
        for k, r in enumerate(rows, 1):
            if feedback.isCanceled():
                break
            base = _safe_name(r["name"]) or _safe_name(
                os.path.splitext(os.path.basename(r["source_path"]))[0]
            )
            out_path = os.path.join(out_dir, f"{base}_clip.tif")
            feedback.pushInfo(f"[{k}/{n}] Recortando -> {out_path}")
            _warp_one(
                src_path=r["source_path"],
                src_band=int(r["band_idx"]),
                out_path=out_path,
                cutline_path=cutline_path, cutline_layer=cutline_layer,
                crop_to_cutline=crop_to_cutline,
                bbox=bbox, bbox_srs=bbox_srs,
                dst_srs=dst_srs, resample=resample, compress=compress,
                feedback=feedback,
            )
            # Asignar nombres a las bandas del output.
            if r["band_idx"] == 0:
                # Todas: heredar las descriptions originales.
                desc = _read_band_descriptions(r["source_path"])
                names = [desc.get(i, f"band_{i}") for i in
                         range(1, r["nbands"] + 1)]
            else:
                names = [r["name"] or _default_name(
                    r["source_path"], r["band_idx"]
                )]
            _apply_band_descriptions(out_path, names)
            salidas.append(out_path)
            feedback.setProgress(int(k * 95 / n))

    elif mode_output == 1:
        # MODO 1: un archivo por banda.
        for k, r in enumerate(rows, 1):
            if feedback.isCanceled():
                break
            if r["band_idx"] == 0:
                bands_to_export = list(range(1, r["nbands"] + 1))
                desc = _read_band_descriptions(r["source_path"])
            else:
                bands_to_export = [int(r["band_idx"])]
                desc = {bands_to_export[0]: r["name"]}
            base = _safe_name(r["name"]) or _safe_name(
                os.path.splitext(os.path.basename(r["source_path"]))[0]
            )
            for b in bands_to_export:
                if feedback.isCanceled():
                    break
                bname = _safe_name(desc.get(b, f"band_{b}")) or f"band_{b}"
                out_path = os.path.join(out_dir, f"{base}_{bname}_clip.tif")
                feedback.pushInfo(
                    f"[{k}/{n}] banda {b} ({bname}) -> {out_path}"
                )
                _warp_one(
                    src_path=r["source_path"],
                    src_band=b,
                    out_path=out_path,
                    cutline_path=cutline_path, cutline_layer=cutline_layer,
                    crop_to_cutline=crop_to_cutline,
                    bbox=bbox, bbox_srs=bbox_srs,
                    dst_srs=dst_srs, resample=resample, compress=compress,
                    feedback=feedback,
                )
                _apply_band_descriptions(out_path, [bname])
                salidas.append(out_path)
            feedback.setProgress(int(k * 95 / n))

    else:
        # MODO 2: stack multibanda combinado.
        # 1. Para cada (raster, banda) extraer recortado a un GeoTIFF
        #    temporal de una sola banda.
        # 2. Construir VRT con separate=True.
        # 3. Translate a GeoTIFF final con compresion.
        # 4. Asignar nombres a las bandas finales.
        feedback.pushInfo("Generando recortes temporales por banda...")
        flat_paths = []
        flat_names = []
        out_dir_tmp = os.path.dirname(out_stack_path) or os.getcwd()
        os.makedirs(out_dir_tmp, exist_ok=True)
        for k, r in enumerate(rows, 1):
            if feedback.isCanceled():
                break
            if r["band_idx"] == 0:
                bs = list(range(1, r["nbands"] + 1))
                desc = _read_band_descriptions(r["source_path"])
            else:
                bs = [int(r["band_idx"])]
                desc = {bs[0]: r["name"]}
            for bidx, b in enumerate(bs, start=1):
                if feedback.isCanceled():
                    break
                tmp_path = os.path.join(
                    out_dir_tmp,
                    f"_geomaticape_clip_tmp_{k:02d}_{bidx:02d}.tif"
                )
                tmp_files.append(tmp_path)
                _warp_one(
                    src_path=r["source_path"], src_band=b,
                    out_path=tmp_path,
                    cutline_path=cutline_path, cutline_layer=cutline_layer,
                    crop_to_cutline=crop_to_cutline,
                    bbox=bbox, bbox_srs=bbox_srs,
                    dst_srs=dst_srs, resample=resample, compress="NONE",
                    feedback=feedback,
                )
                flat_paths.append(tmp_path)
                if r["band_idx"] != 0:
                    flat_names.append(_safe_name(r["name"]) or
                                       _default_name(r["source_path"], b))
                else:
                    nm = desc.get(b, f"band_{b}")
                    flat_names.append(_safe_name(nm) or f"band_{b}")
            feedback.setProgress(int(k * 70 / n))

        if feedback.isCanceled():
            for v in tmp_files:
                try:
                    os.remove(v)
                except OSError:
                    pass
            raise RuntimeError("Operacion cancelada por el usuario.")

        # Construir VRT separate.
        vrt_path = out_stack_path + "_stack.vrt"
        feedback.pushInfo("Construyendo VRT multibanda apilado...")
        vrt_opts = gdal.BuildVRTOptions(separate=True)
        vrt_ds = gdal.BuildVRT(vrt_path, flat_paths, options=vrt_opts)
        if vrt_ds is None:
            for v in tmp_files:
                try:
                    os.remove(v)
                except OSError:
                    pass
            raise RuntimeError("No se pudo construir el VRT multibanda.")
        vrt_ds = None

        feedback.pushInfo("Escribiendo GeoTIFF stack final...")
        creation = ["TILED=YES", "BIGTIFF=IF_SAFER"]
        if compress and compress != "NONE":
            creation.append(f"COMPRESS={compress}")
        gdal.Translate(out_stack_path, vrt_path, creationOptions=creation)
        feedback.setProgress(92)

        _apply_band_descriptions(out_stack_path, flat_names)
        salidas.append(out_stack_path)

        # Limpieza
        try:
            os.remove(vrt_path)
        except OSError:
            pass
        for v in tmp_files:
            try:
                os.remove(v)
            except OSError:
                pass

    feedback.pushInfo("=" * 64)
    feedback.pushInfo(f"OK - Archivos generados: {len(salidas)}")
    for s in salidas:
        feedback.pushInfo(f"  -> {s}")
    feedback.pushInfo("=" * 64)
    feedback.setProgress(100)
    return salidas


# ---------------------------------------------------------------------------
# Picker de capas vectoriales QGIS
# ---------------------------------------------------------------------------

class _QGISVectorPickerDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Selecciona capa vectorial (cutline)")
        self.resize(560, 380)
        self._layers = []
        self._build_ui()
        self._populate()

    def _build_ui(self):
        v = QVBoxLayout(self)
        info = QLabel(
            "Selecciona la capa vectorial poligonal del proyecto QGIS que "
            "definira la zona de estudio (cutline)."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        v.addWidget(self.list, 1)

        self.lbl_status = QLabel("")
        v.addWidget(self.lbl_status)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _populate(self):
        try:
            from qgis.core import QgsProject, QgsVectorLayer, QgsWkbTypes
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                    geom_type = QgsWkbTypes.geometryType(lyr.wkbType())
                    if geom_type != QgsWkbTypes.PolygonGeometry:
                        continue
                    self._layers.append(lyr)
                    item = QListWidgetItem(
                        f"{lyr.name()}  -  {lyr.featureCount()} features"
                    )
                    item.setToolTip(lyr.source())
                    item.setData(Qt.UserRole, len(self._layers) - 1)
                    self.list.addItem(item)
        except Exception as e:
            self.lbl_status.setText(f"Error leyendo capas QGIS: {e}")
            return
        if not self._layers:
            self.lbl_status.setText(
                "(No hay capas vectoriales POLIGONALES en el proyecto QGIS.)"
            )
        else:
            self.lbl_status.setText(
                f"{len(self._layers)} capa(s) poligonal(es) disponibles."
            )

    def get_selected_layer(self):
        rows = self.list.selectedItems()
        if not rows:
            return None
        idx = rows[0].data(Qt.UserRole)
        if idx is None or idx < 0 or idx >= len(self._layers):
            return None
        return self._layers[idx]


# ---------------------------------------------------------------------------
# Dialogo principal
# ---------------------------------------------------------------------------

class RecortarRastersZonaDialog(QDialog):

    COLS = ("#", "Origen (capa o archivo)", "Banda", "Nombre")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recortar raster por zona de estudio - Geomaticape")
        self.resize(960, 720)

        # Estado del cutline
        self._cutline_path = ""
        self._cutline_layer = ""
        self._cutline_label = ""
        self._cutline_srs = ""

        # Estado de bbox
        self._bbox_srs = ""

        self._build_ui()

    # ------------------- UI ---------------------------------------------

    def _build_ui(self):
        v = QVBoxLayout(self)

        info = QLabel(
            "<b>Recortar raster(s) por zona de estudio</b><br>"
            "Recorta uno o varios raster usando una capa vectorial "
            "(cutline) o un rectangulo de extension (bbox). Cada raster "
            "puede aportar TODAS sus bandas o una banda especifica. "
            "Tres modos de salida: un archivo por entrada, un archivo "
            "por banda o un solo stack multibanda."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        # ---- Grupo: rasters a recortar ----
        gb_in = QGroupBox("Raster(s) a recortar")
        gb_in_l = QVBoxLayout(gb_in)

        self.table = QTableWidget(0, 4, gb_in)
        self.table.setHorizontalHeaderLabels(self.COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 220)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
            | QAbstractItemView.AnyKeyPressed
        )
        gb_in_l.addWidget(self.table, 1)

        row_btn = QHBoxLayout()
        self.btn_add_file = QPushButton("Agregar archivo(s)...")
        self.btn_add_file.clicked.connect(self._on_add_file)
        self.btn_add_qgis = QPushButton("Agregar capa(s) QGIS...")
        self.btn_add_qgis.clicked.connect(self._on_add_qgis)
        self.btn_add_full = QPushButton("Cargar imagen completa (todas las bandas)...")
        self.btn_add_full.clicked.connect(self._on_add_full)
        self.btn_remove = QPushButton("Quitar")
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_up = QPushButton("Subir")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = QPushButton("Bajar")
        self.btn_down.clicked.connect(lambda: self._move(+1))
        self.btn_clear = QPushButton("Limpiar")
        self.btn_clear.clicked.connect(self._on_clear)
        for b in (self.btn_add_file, self.btn_add_qgis, self.btn_add_full,
                  self.btn_remove, self.btn_up, self.btn_down, self.btn_clear):
            row_btn.addWidget(b)
        row_btn.addStretch(1)
        gb_in_l.addLayout(row_btn)

        v.addWidget(gb_in, 1)

        # ---- Grupo: zona de estudio ----
        gb_zone = QGroupBox("Zona de estudio")
        gb_zone_l = QFormLayout(gb_zone)

        self.combo_mode_zone = QComboBox()
        self.combo_mode_zone.addItems([
            "Por geometria (cutline poligonal)",
            "Por extension (bbox xmin/ymin/xmax/ymax)",
        ])
        self.combo_mode_zone.currentIndexChanged.connect(self._on_zone_mode_changed)
        gb_zone_l.addRow("Modo:", self.combo_mode_zone)

        self.stack_zone = QStackedWidget()

        # ---- Pagina 0: cutline ----
        pg_cut = QWidget()
        pg_cut_l = QFormLayout(pg_cut)
        pg_cut_l.setContentsMargins(0, 0, 0, 0)
        cut_widget = QWidget()
        ch = QHBoxLayout(cut_widget); ch.setContentsMargins(0, 0, 0, 0)
        self.lbl_cutline = QLineEdit()
        self.lbl_cutline.setReadOnly(True)
        self.lbl_cutline.setPlaceholderText("(sin seleccionar)")
        self.btn_cut_qgis = QPushButton("Capa QGIS...")
        self.btn_cut_qgis.clicked.connect(self._on_cut_from_qgis)
        self.btn_cut_file = QPushButton("Archivo...")
        self.btn_cut_file.clicked.connect(self._on_cut_from_file)
        ch.addWidget(self.lbl_cutline, 1)
        ch.addWidget(self.btn_cut_qgis)
        ch.addWidget(self.btn_cut_file)
        pg_cut_l.addRow("Capa vectorial:", cut_widget)

        self.chk_crop = QCheckBox("Cortar exactamente al poligono "
                                   "(cropToCutline)")
        self.chk_crop.setChecked(True)
        pg_cut_l.addRow("", self.chk_crop)

        self.stack_zone.addWidget(pg_cut)

        # ---- Pagina 1: bbox ----
        pg_bbox = QWidget()
        pg_bbox_l = QFormLayout(pg_bbox)
        pg_bbox_l.setContentsMargins(0, 0, 0, 0)

        bbox_widget = QWidget()
        bh = QHBoxLayout(bbox_widget); bh.setContentsMargins(0, 0, 0, 0)
        self.spin_xmin = self._mk_spin()
        self.spin_ymin = self._mk_spin()
        self.spin_xmax = self._mk_spin()
        self.spin_ymax = self._mk_spin()
        bh.addWidget(QLabel("xmin")); bh.addWidget(self.spin_xmin)
        bh.addWidget(QLabel("ymin")); bh.addWidget(self.spin_ymin)
        bh.addWidget(QLabel("xmax")); bh.addWidget(self.spin_xmax)
        bh.addWidget(QLabel("ymax")); bh.addWidget(self.spin_ymax)
        pg_bbox_l.addRow("Extension:", bbox_widget)

        srs_widget = QWidget()
        sh = QHBoxLayout(srs_widget); sh.setContentsMargins(0, 0, 0, 0)
        self.line_bbox_srs = QLineEdit()
        self.line_bbox_srs.setPlaceholderText(
            "EPSG:32718, WKT o vacio = mismo CRS que el primer raster"
        )
        self.btn_bbox_from_layer = QPushButton("Tomar de capa QGIS...")
        self.btn_bbox_from_layer.clicked.connect(self._on_bbox_from_layer)
        sh.addWidget(self.line_bbox_srs, 1)
        sh.addWidget(self.btn_bbox_from_layer)
        pg_bbox_l.addRow("CRS del bbox:", srs_widget)

        self.stack_zone.addWidget(pg_bbox)

        gb_zone_l.addRow("", self.stack_zone)

        v.addWidget(gb_zone)

        # ---- Grupo: opciones y salida ----
        gb_opts = QGroupBox("Opciones y salida")
        f = QFormLayout(gb_opts)

        self.combo_output_mode = QComboBox()
        self.combo_output_mode.addItems(OUTPUT_MODE_OPTIONS)
        self.combo_output_mode.currentIndexChanged.connect(self._on_output_mode_changed)
        f.addRow("Modo de salida:", self.combo_output_mode)

        self.combo_resample = QComboBox()
        self.combo_resample.addItems(CUTLINE_RESAMPLE)
        self.combo_resample.setCurrentText("nearest")
        f.addRow("Remuestreo:", self.combo_resample)

        self.combo_compress = QComboBox()
        self.combo_compress.addItems(COMPRESS_OPTIONS)
        f.addRow("Compresion GeoTIFF:", self.combo_compress)

        self.line_dst_srs = QLineEdit()
        self.line_dst_srs.setPlaceholderText(
            "vacio = mantener CRS original; o EPSG:32718 / WKT"
        )
        f.addRow("CRS de salida (opcional):", self.line_dst_srs)

        # Output dir / output stack file (cambiamos segun modo)
        self.out_dir_widget = QWidget()
        oh = QHBoxLayout(self.out_dir_widget); oh.setContentsMargins(0, 0, 0, 0)
        self.line_out_dir = QLineEdit()
        self.btn_out_dir = QPushButton("...")
        self.btn_out_dir.setFixedWidth(34)
        self.btn_out_dir.clicked.connect(self._on_out_dir)
        oh.addWidget(self.line_out_dir, 1)
        oh.addWidget(self.btn_out_dir)
        f.addRow("Carpeta de salida:", self.out_dir_widget)

        self.out_stack_widget = QWidget()
        sh2 = QHBoxLayout(self.out_stack_widget); sh2.setContentsMargins(0, 0, 0, 0)
        self.line_out_stack = QLineEdit()
        self.btn_out_stack = QPushButton("...")
        self.btn_out_stack.setFixedWidth(34)
        self.btn_out_stack.clicked.connect(self._on_out_stack)
        sh2.addWidget(self.line_out_stack, 1)
        sh2.addWidget(self.btn_out_stack)
        f.addRow("Stack multibanda (.tif):", self.out_stack_widget)

        v.addWidget(gb_opts)

        # ---- OK / Cancel ----
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Ejecutar recorte")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self._on_zone_mode_changed(0)
        self._on_output_mode_changed(0)

    def _mk_spin(self):
        s = QDoubleSpinBox()
        s.setDecimals(4)
        s.setRange(-1e12, 1e12)
        s.setMaximumWidth(140)
        s.setValue(0.0)
        return s

    # ------------------- helpers de filas --------------------------------

    def _append_row(self, source_path, source_label, nbands, band_idx, name):
        nb = max(1, int(nbands))
        bi = max(0, min(int(band_idx), nb))  # 0 = Todas

        r = self.table.rowCount()
        self.table.insertRow(r)

        it_n = QTableWidgetItem(str(r + 1))
        it_n.setFlags(it_n.flags() & ~Qt.ItemIsEditable)
        it_n.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(r, 0, it_n)

        it_p = QTableWidgetItem(source_label)
        it_p.setToolTip(source_path)
        it_p.setData(Qt.UserRole, source_path)
        it_p.setData(Qt.UserRole + 1, nb)
        it_p.setFlags(it_p.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, 1, it_p)

        combo = QComboBox()
        combo.addItem("Todas")
        for i in range(1, nb + 1):
            combo.addItem(str(i))
        combo.setCurrentIndex(bi)  # 0 = Todas
        self.table.setCellWidget(r, 2, combo)

        it_name = QTableWidgetItem(name)
        it_name.setToolTip("Doble click para editar el nombre / sufijo de "
                            "salida")
        self.table.setItem(r, 3, it_name)

        self.table.setCurrentCell(r, 3)

    def _snapshot_rows(self):
        rows = []
        for r in range(self.table.rowCount()):
            it_p = self.table.item(r, 1)
            combo = self.table.cellWidget(r, 2)
            it_n = self.table.item(r, 3)
            if it_p is None:
                continue
            band_text = combo.currentText() if combo else "Todas"
            band_idx = 0 if band_text == "Todas" else int(band_text)
            rows.append({
                "source_path":  it_p.data(Qt.UserRole) or "",
                "source_label": it_p.text() or "",
                "nbands":       int(it_p.data(Qt.UserRole + 1) or 1),
                "band_idx":     band_idx,
                "name":         (it_n.text() if it_n else "").strip(),
            })
        return rows

    def _populate_from_rows(self, rows):
        self.table.setRowCount(0)
        for d in rows:
            self._append_row(
                d["source_path"], d["source_label"],
                d["nbands"], d["band_idx"], d["name"]
            )

    def _renumber(self):
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is None:
                it = QTableWidgetItem()
                self.table.setItem(r, 0, it)
            it.setText(str(r + 1))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setTextAlignment(Qt.AlignCenter)

    # ------------------- acciones rasters --------------------------------

    def _on_add_file(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecciona uno o mas raster",
            "",
            "Raster (*.tif *.tiff *.img *.vrt *.jp2 *.dat);;Todos (*.*)"
        )
        for f in files:
            nb = _band_count(f)
            self._append_row(
                source_path=f,
                source_label=os.path.basename(f),
                nbands=nb,
                band_idx=0,  # Todas por defecto
                name=_safe_name(os.path.splitext(os.path.basename(f))[0]),
            )

    def _on_add_qgis(self):
        dlg = _QGISLayerPickerDialog(parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        items = dlg.get_selected()
        if not items:
            return
        for d in items:
            self._append_row(
                source_path=d["source_path"],
                source_label=d["source_label"],
                nbands=d["nbands"],
                band_idx=0,  # Todas por defecto al recortar
                name=_safe_name(
                    os.path.splitext(os.path.basename(d["source_path"]))[0]
                ),
            )

    def _on_add_full(self):
        """Carga UN raster y agrega una fila por cada una de sus bandas
        con el nombre detectado."""
        msg = QMessageBox(self)
        msg.setWindowTitle("Cargar imagen completa")
        msg.setIcon(QMessageBox.Question)
        msg.setText("De donde tomas la imagen multibanda?")
        b_q = msg.addButton("Capa QGIS...", QMessageBox.AcceptRole)
        b_f = msg.addButton("Archivo...", QMessageBox.AcceptRole)
        b_c = msg.addButton("Cancelar", QMessageBox.RejectRole)
        msg.exec_()
        clicked = msg.clickedButton()
        if clicked is b_c or clicked is None:
            return

        path = label = None
        nb = 1
        if clicked is b_q:
            dlg = _QGISLayerPickerDialog(parent=self)
            if dlg.exec_() != QDialog.Accepted:
                return
            items = dlg.get_selected()
            if not items:
                return
            d = items[0]
            path = d["source_path"]; label = d["source_label"]; nb = d["nbands"]
        else:
            f, _ = QFileDialog.getOpenFileName(
                self, "Selecciona un raster",
                "",
                "Raster (*.tif *.tiff *.img *.vrt *.jp2 *.dat);;Todos (*.*)"
            )
            if not f:
                return
            path = f; label = os.path.basename(f); nb = _band_count(f)

        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is None:
            QMessageBox.critical(self, "Recortar",
                                  f"GDAL no pudo abrir: {path}")
            return
        descs = []
        base = os.path.splitext(os.path.basename(path))[0]
        for b in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(b)
            d = (band.GetDescription() or "").strip() if band else ""
            if not d:
                d = f"{base}_b{b}"
            descs.append(_safe_name(d))
        ds = None

        for i, nm in enumerate(descs, start=1):
            self._append_row(
                source_path=path,
                source_label=label,
                nbands=nb,
                band_idx=i,  # banda especifica
                name=nm,
            )

    def _on_remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._renumber()

    def _on_clear(self):
        self.table.setRowCount(0)

    def _move(self, delta):
        sel_rows = sorted(
            {i.row() for i in self.table.selectedIndexes()},
            reverse=(delta > 0)
        )
        if not sel_rows:
            return
        rows = self._snapshot_rows()
        n = len(rows)
        moved = []
        for r in sel_rows:
            new_r = r + delta
            if 0 <= new_r < n and new_r not in moved:
                rows[r], rows[new_r] = rows[new_r], rows[r]
                moved.append(new_r)
            else:
                moved.append(r)
        self._populate_from_rows(rows)
        self.table.clearSelection()
        for r in moved:
            self.table.selectRow(r)

    # ------------------- acciones zona de estudio ------------------------

    def _on_zone_mode_changed(self, idx):
        self.stack_zone.setCurrentIndex(idx)

    def _on_output_mode_changed(self, idx):
        # 0 = un archivo por entrada -> carpeta
        # 1 = un archivo por banda    -> carpeta
        # 2 = stack                   -> archivo
        is_stack = (idx == 2)
        self.out_dir_widget.setVisible(not is_stack)
        self.out_stack_widget.setVisible(is_stack)
        # Hide/show their corresponding labels by walking the form
        # (visibilidad gestionada por el widget propio).

    def _on_cut_from_qgis(self):
        dlg = _QGISVectorPickerDialog(parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        lyr = dlg.get_selected_layer()
        if lyr is None:
            return
        try:
            path, lname = _resolve_vector_source(lyr)
        except Exception as e:
            QMessageBox.critical(self, "Recortar",
                                  f"No se pudo resolver el cutline: {e}")
            return
        self._cutline_path = path
        self._cutline_layer = lname or ""
        self._cutline_label = f"{lyr.name()} (capa QGIS)"
        try:
            self._cutline_srs = lyr.crs().toWkt() if lyr.crs() else ""
        except Exception:
            self._cutline_srs = ""
        self.lbl_cutline.setText(
            self._cutline_label
            + (f"   [layer={lname}]" if lname else "")
        )

    def _on_cut_from_file(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Selecciona el shapefile / GeoPackage del cutline",
            "",
            "Vector (*.shp *.gpkg *.geojson *.json *.kml *.kmz);;Todos (*.*)"
        )
        if not f:
            return
        self._cutline_path = f
        self._cutline_layer = ""
        self._cutline_label = os.path.basename(f)
        self._cutline_srs = ""
        self.lbl_cutline.setText(self._cutline_label)

    def _on_bbox_from_layer(self):
        # Mostramos un picker que liste TODAS las capas del proyecto para
        # tomarles la extent. Usamos un mini-dialogo simple.
        try:
            from qgis.core import QgsProject
            layers = list(QgsProject.instance().mapLayers().values())
        except Exception:
            layers = []
        if not layers:
            QMessageBox.information(self, "Recortar",
                                     "No hay capas cargadas en el proyecto.")
            return

        # Listar en un QInputDialog
        from qgis.PyQt.QtWidgets import QInputDialog
        names = [l.name() for l in layers]
        item, ok = QInputDialog.getItem(
            self, "Tomar extension de capa",
            "Selecciona la capa cuyo extent y CRS se usaran como bbox:",
            names, 0, False
        )
        if not ok or not item:
            return
        lyr = next((l for l in layers if l.name() == item), None)
        if lyr is None:
            return
        try:
            ext = lyr.extent()
            self.spin_xmin.setValue(ext.xMinimum())
            self.spin_ymin.setValue(ext.yMinimum())
            self.spin_xmax.setValue(ext.xMaximum())
            self.spin_ymax.setValue(ext.yMaximum())
            try:
                authid = lyr.crs().authid() if lyr.crs() else ""
                if authid:
                    self.line_bbox_srs.setText(authid)
                else:
                    self.line_bbox_srs.setText(
                        lyr.crs().toWkt() if lyr.crs() else ""
                    )
            except Exception:
                pass
        except Exception as e:
            QMessageBox.critical(self, "Recortar",
                                  f"No se pudo leer la extension: {e}")

    # ------------------- salida ------------------------------------------

    def _on_out_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Carpeta de salida", self.line_out_dir.text().strip()
        )
        if d:
            self.line_out_dir.setText(d)

    def _on_out_stack(self):
        f, _ = QFileDialog.getSaveFileName(
            self, "GeoTIFF de stack multibanda",
            self.line_out_stack.text().strip(),
            "GeoTIFF (*.tif *.tiff)"
        )
        if f:
            if not f.lower().endswith((".tif", ".tiff")):
                f += ".tif"
            self.line_out_stack.setText(f)

    # ------------------- ejecutar ----------------------------------------

    def _on_ok(self):
        # Cerrar edicion en curso de la tabla
        cur = self.table.currentItem()
        if cur is not None:
            try:
                self.table.closePersistentEditor(cur)
            except Exception:
                pass

        rows = self._snapshot_rows()
        rows = [r for r in rows if r["source_path"]]
        if not rows:
            QMessageBox.warning(self, "Recortar",
                                 "Agrega al menos un raster a la tabla.")
            return

        zone_mode = self.combo_mode_zone.currentIndex()
        cutline_path = ""
        cutline_layer = ""
        crop_to_cutline = True
        bbox = None
        bbox_srs = ""

        if zone_mode == 0:
            cutline_path = self._cutline_path
            cutline_layer = self._cutline_layer
            crop_to_cutline = self.chk_crop.isChecked()
            if not cutline_path:
                QMessageBox.warning(self, "Recortar",
                                     "Selecciona la capa vectorial cutline.")
                return
        else:
            xmin = self.spin_xmin.value()
            ymin = self.spin_ymin.value()
            xmax = self.spin_xmax.value()
            ymax = self.spin_ymax.value()
            if not (xmax > xmin and ymax > ymin):
                QMessageBox.warning(self, "Recortar",
                                     "BBox invalido: xmax debe ser > xmin "
                                     "y ymax > ymin.")
                return
            bbox = (xmin, ymin, xmax, ymax)
            bbox_srs = self.line_bbox_srs.text().strip()

        mode_output = self.combo_output_mode.currentIndex()
        out_dir = self.line_out_dir.text().strip()
        out_stack = self.line_out_stack.text().strip()

        if mode_output in (0, 1) and not out_dir:
            QMessageBox.warning(self, "Recortar",
                                 "Define la carpeta de salida.")
            return
        if mode_output == 2 and not out_stack:
            QMessageBox.warning(self, "Recortar",
                                 "Define el archivo .tif de stack de salida.")
            return
        if mode_output == 2 and not out_stack.lower().endswith((".tif", ".tiff")):
            out_stack += ".tif"
            self.line_out_stack.setText(out_stack)

        resample = self.combo_resample.currentText()
        compress = self.combo_compress.currentText()
        dst_srs = self.line_dst_srs.text().strip()

        progress = QProgressDialog("Procesando...", "Cancelar", 0, 100, self)
        progress.setWindowTitle("Recortar raster por zona de estudio")
        progress.setWindowModality(Qt.WindowModal)
        progress.setAutoClose(True)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        feedback = _DialogFeedback(progress)
        try:
            salidas = ejecutar_recorte(
                rows=rows,
                mode_output=mode_output,
                out_dir=out_dir,
                out_stack_path=out_stack,
                cutline_path=cutline_path,
                cutline_layer=cutline_layer,
                crop_to_cutline=crop_to_cutline,
                bbox=bbox, bbox_srs=bbox_srs,
                dst_srs=dst_srs, resample=resample, compress=compress,
                feedback=feedback,
            )
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Recortar - Error", str(e))
            return

        progress.close()

        # Cargar resultados en QGIS si esta disponible.
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            for s in salidas:
                lyr = QgsRasterLayer(s, os.path.basename(s))
                if lyr.isValid():
                    QgsProject.instance().addMapLayer(lyr)
        except Exception:
            pass

        QMessageBox.information(
            self, "Recortar",
            f"Recorte generado: {len(salidas)} archivo(s).\n\n"
            + "\n".join(f"  - {s}" for s in salidas[:8])
            + ("\n  ..." if len(salidas) > 8 else "")
        )
        self.accept()


# ---------------------------------------------------------------------------
# Wrapper invocado desde el menu Geomaticape -> Procesamiento
# ---------------------------------------------------------------------------

class RecortarRastersZona:
    """Lanzador desde el menu del plugin."""

    def __init__(self, iface=None):
        self.iface = iface

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "poligonos_tabla.png"))

    def run(self):
        parent = None
        try:
            from qgis.utils import iface as _qgis_iface
            if _qgis_iface is not None:
                parent = _qgis_iface.mainWindow()
        except Exception:
            parent = None
        dlg = RecortarRastersZonaDialog(parent=parent)
        dlg.exec_()
