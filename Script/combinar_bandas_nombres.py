"""
Combinar bandas con nombres
============================
Dialogo Qt personalizado para apilar bandas de varios raster en un GeoTIFF
multibanda con nombres descriptivos (Red, NIR, SWIR1...). El nombre se
edita AL COSTADO de cada raster en la columna "Nombre de la banda".

Fuentes admitidas:
  * Archivos raster del disco (boton "Agregar raster...").
  * Capas raster del proyecto QGIS (boton "Agregar capa(s) QGIS...").

Para cada entrada se elige la BANDA del raster a usar (combo "Banda" en
la fila). Por defecto banda 1; si el origen tiene N bandas, el combo
permite escoger 1..N. El nombre por defecto se autodetecta desde
band[N].GetDescription() o, si esta vacio, el nombre del archivo.

Si los raster no comparten grilla / CRS / extent, los reproyecta y
remuestrea automaticamente al primero (raster de referencia) usando
gdal.Warp con el metodo elegido por el usuario.

Cada banda se etiqueta con band.SetDescription(nombre) en el GeoTIFF
final, de modo que QGIS, ArcGIS, ENVI y SNAP muestran "Red, NIR, ..."
en lugar de "Band 1, Band 2, ...".

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.14
Grupo : Procesamiento
"""

import os
import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton, QLabel, QLineEdit,
    QFileDialog, QMessageBox, QComboBox, QDialogButtonBox,
    QProgressDialog, QApplication, QWidget, QAbstractItemView,
)
from osgeo import gdal


RESAMPLE_METHODS = [
    "nearest", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode",
]
COMPRESS_OPTIONS = ["LZW", "DEFLATE", "PACKBITS", "ZSTD", "NONE"]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _safe_name(s):
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(s)).strip("_")
    return s or "Band"


def _grid_info(path):
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


def _grids_match(a, b, tol=1e-6):
    if a["cols"] != b["cols"] or a["rows"] != b["rows"]:
        return False
    if (a["proj"] or "") != (b["proj"] or ""):
        return False
    for i in range(6):
        if abs(a["gt"][i] - b["gt"][i]) > tol:
            return False
    return True


def _band_count(path):
    """Cuenta bandas con GDAL (1 si falla)."""
    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is not None:
            n = ds.RasterCount
            ds = None
            return max(1, int(n))
    except Exception:
        pass
    return 1


def _detect_band_name_at(path, band_idx):
    """Lee la descripcion de la banda N; None si no hay."""
    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is not None:
            if 1 <= band_idx <= ds.RasterCount:
                b = ds.GetRasterBand(band_idx)
                desc = b.GetDescription() if b is not None else ""
                ds = None
                if desc:
                    return _safe_name(desc)
            ds = None
    except Exception:
        pass
    return None


def _default_name(path, band_idx=1, layer_name=None):
    """Nombre por defecto para una entrada raster + banda."""
    n = _detect_band_name_at(path, band_idx)
    if n:
        return n
    if layer_name:
        if band_idx == 1:
            return _safe_name(layer_name)
        return _safe_name(f"{layer_name}_b{band_idx}")
    base = os.path.splitext(os.path.basename(path))[0]
    if band_idx == 1:
        return _safe_name(base)
    return _safe_name(f"{base}_b{band_idx}")


def _cleanup(paths):
    for p in paths:
        if not p:
            continue
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Logica de procesamiento
# ---------------------------------------------------------------------------

def ejecutar_combinacion(paths, bands, names, out_path, resample, compress,
                         feedback):
    """Apila paths[bands] en out_path con nombres dados, alineando al primero."""
    if not paths or len(paths) < 2:
        raise RuntimeError("Selecciona al menos 2 raster.")
    if not (len(paths) == len(bands) == len(names)):
        raise RuntimeError(
            "Listas inconsistentes (paths/bands/names tienen distinta longitud)."
        )

    ref = _grid_info(paths[0])
    if not ref["proj"]:
        feedback.pushWarning(
            "Raster de referencia sin CRS; la salida tampoco tendra CRS."
        )

    feedback.pushInfo("=" * 60)
    feedback.pushInfo("Combinar bandas con nombres")
    feedback.pushInfo(
        f"Referencia: {os.path.basename(paths[0])}  "
        f"({ref['cols']}x{ref['rows']} px)"
    )
    feedback.pushInfo("Orden y nombres:")
    for i, (p, b, n) in enumerate(zip(paths, bands, names), 1):
        feedback.pushInfo(
            f"  Banda {i:2d}: {n:<14s}  <-  {os.path.basename(p)} "
            f"(banda origen: {b})"
        )
    feedback.pushInfo("=" * 60)

    out_dir = os.path.dirname(out_path) or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)

    ref_xmin = ref["gt"][0]
    ref_ymax = ref["gt"][3]
    ref_xmax = ref_xmin + ref["cols"] * ref["gt"][1]
    ref_ymin = ref_ymax + ref["rows"] * ref["gt"][5]
    ref_xres = abs(ref["gt"][1])
    ref_yres = abs(ref["gt"][5])

    aligned_paths = []
    tmp_files = []
    n = len(paths)
    for k, (p, bnd) in enumerate(zip(paths, bands), start=1):
        if feedback.isCanceled():
            break
        info = _grid_info(p)
        if bnd < 1 or bnd > info["nbands"]:
            feedback.pushWarning(
                f"  '{os.path.basename(p)}': banda {bnd} fuera de rango "
                f"(tiene {info['nbands']}). Se usa banda 1."
            )
            bnd = 1

        # Caso rapido: archivo de 1 banda, banda 1, grilla identica.
        if info["nbands"] == 1 and bnd == 1 and _grids_match(info, ref):
            aligned_paths.append(p)
            feedback.pushInfo(
                f"  [{k}/{n}] Grilla OK -> {os.path.basename(p)} (banda 1)"
            )
        else:
            tmp = os.path.join(out_dir, f"_geomaticape_align_{k:02d}.tif")
            tmp_files.append(tmp)
            try:
                gdal.Warp(
                    tmp, p, format="GTiff", srcBands=[bnd],
                    dstSRS=ref["proj"] or None,
                    outputBounds=(ref_xmin, ref_ymin, ref_xmax, ref_ymax),
                    xRes=ref_xres, yRes=ref_yres,
                    width=ref["cols"], height=ref["rows"],
                    resampleAlg=resample, multithread=True,
                    creationOptions=[
                        "TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"
                    ],
                )
            except TypeError:
                tmp_b1 = tmp + ".b1.tif"
                tmp_files.append(tmp_b1)
                gdal.Translate(tmp_b1, p, bandList=[bnd])
                gdal.Warp(
                    tmp, tmp_b1, format="GTiff",
                    dstSRS=ref["proj"] or None,
                    outputBounds=(ref_xmin, ref_ymin, ref_xmax, ref_ymax),
                    xRes=ref_xres, yRes=ref_yres,
                    width=ref["cols"], height=ref["rows"],
                    resampleAlg=resample, multithread=True,
                    creationOptions=[
                        "TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"
                    ],
                )
            aligned_paths.append(tmp)
            feedback.pushInfo(
                f"  [{k}/{n}] Alineado: {os.path.basename(p)} (banda {bnd}) "
                f"-> banda {k} ({names[k-1]})"
            )
        feedback.setProgress(int(k * 80 / n))

    if feedback.isCanceled():
        _cleanup(tmp_files)
        raise RuntimeError("Operacion cancelada por el usuario.")

    feedback.pushInfo("Construyendo VRT multibanda...")
    vrt_path = os.path.splitext(out_path)[0] + "_stack.vrt"
    vrt_opts = gdal.BuildVRTOptions(
        separate=True, resolution="user",
        xRes=ref_xres, yRes=ref_yres,
        outputBounds=(ref_xmin, ref_ymin, ref_xmax, ref_ymax),
    )
    vrt_ds = gdal.BuildVRT(vrt_path, aligned_paths, options=vrt_opts)
    if vrt_ds is None:
        _cleanup(tmp_files + [vrt_path])
        raise RuntimeError("No se pudo construir el VRT multibanda.")
    vrt_ds = None  # flush

    feedback.pushInfo("Escribiendo GeoTIFF final...")
    creation = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if compress != "NONE":
        creation.append(f"COMPRESS={compress}")
    gdal.Translate(out_path, vrt_path, creationOptions=creation)
    feedback.setProgress(95)

    ds_out = gdal.Open(out_path, gdal.GA_Update)
    if ds_out is None:
        _cleanup(tmp_files + [vrt_path])
        raise RuntimeError(f"No se pudo abrir el raster final: {out_path}")
    for i, nm in enumerate(names, 1):
        band = ds_out.GetRasterBand(i)
        band.SetDescription(nm)
        try:
            band.SetMetadataItem("BAND_NAME", nm)
        except Exception:
            pass
    try:
        ds_out.SetMetadataItem("GEOMATICAPE_BAND_ORDER", ",".join(names))
    except Exception:
        pass
    ds_out.FlushCache()
    ds_out = None

    _cleanup(tmp_files + [vrt_path])

    feedback.pushInfo("=" * 60)
    feedback.pushInfo(f"OK - Raster combinado: {out_path}")
    feedback.pushInfo(f"Bandas: {', '.join(names)}")
    feedback.pushInfo("=" * 60)
    feedback.setProgress(100)
    return out_path


# ---------------------------------------------------------------------------
# Adaptador de feedback contra QProgressDialog
# ---------------------------------------------------------------------------

class _DialogFeedback:
    def __init__(self, progress=None):
        self.progress = progress
        self.log = []

    def _emit(self, msg):
        self.log.append(msg)
        if self.progress:
            self.progress.setLabelText(msg[:160])
            QApplication.processEvents()

    def pushInfo(self, msg):
        self._emit(str(msg))

    def pushWarning(self, msg):
        self._emit("AVISO: " + str(msg))

    def setProgress(self, pct):
        if self.progress:
            self.progress.setValue(int(pct))
            QApplication.processEvents()

    def isCanceled(self):
        return bool(self.progress and self.progress.wasCanceled())


# ---------------------------------------------------------------------------
# Picker de capas QGIS
# ---------------------------------------------------------------------------

class _QGISLayerPickerDialog(QDialog):
    """Dialogo modal para escoger capas raster del proyecto QGIS y la
    banda a usar de cada una."""

    HEADERS = ("Capa raster del proyecto", "# bandas", "Banda a usar")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agregar bandas desde capas QGIS")
        self.resize(680, 440)
        self._build_ui()
        self._populate()

    def _build_ui(self):
        v = QVBoxLayout(self)

        info = QLabel(
            "<b>Marca</b> las capas raster que quieras agregar y elige la "
            "<b>banda</b> a usar de cada una. Cada capa marcada agregara "
            "una fila en la tabla principal."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        self.tbl = QTableWidget(0, 3, self)
        self.tbl.setHorizontalHeaderLabels(self.HEADERS)
        self.tbl.verticalHeader().setVisible(False)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        v.addWidget(self.tbl, 1)

        # Botones de seleccion masiva
        bar = QHBoxLayout()
        self.btn_check_all = QPushButton("Marcar todas")
        self.btn_check_all.clicked.connect(lambda: self._check_all(Qt.Checked))
        self.btn_uncheck_all = QPushButton("Desmarcar todas")
        self.btn_uncheck_all.clicked.connect(lambda: self._check_all(Qt.Unchecked))
        bar.addWidget(self.btn_check_all)
        bar.addWidget(self.btn_uncheck_all)
        bar.addStretch(1)
        v.addLayout(bar)

        self.lbl_status = QLabel("")
        v.addWidget(self.lbl_status)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Agregar marcadas")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _populate(self):
        self.tbl.setRowCount(0)
        layers = []
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsRasterLayer) and lyr.isValid():
                    layers.append(lyr)
        except Exception as e:
            self.lbl_status.setText(
                f"No fue posible leer las capas QGIS: {e}"
            )
            return

        if not layers:
            self.lbl_status.setText(
                "(No hay capas raster cargadas en el proyecto QGIS.)"
            )
            return

        for lyr in layers:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)

            try:
                nb = int(lyr.bandCount())
            except Exception:
                nb = 1
            nb = max(1, nb)

            it_name = QTableWidgetItem(lyr.name())
            it_name.setFlags(it_name.flags() | Qt.ItemIsUserCheckable)
            it_name.setCheckState(Qt.Unchecked)
            it_name.setData(Qt.UserRole, lyr.source())
            it_name.setData(Qt.UserRole + 1, lyr.name())
            it_name.setData(Qt.UserRole + 2, nb)
            it_name.setToolTip(lyr.source())
            self.tbl.setItem(r, 0, it_name)

            it_nb = QTableWidgetItem(str(nb))
            it_nb.setFlags(it_nb.flags() & ~Qt.ItemIsEditable)
            it_nb.setTextAlignment(Qt.AlignCenter)
            self.tbl.setItem(r, 1, it_nb)

            combo = QComboBox()
            for i in range(1, nb + 1):
                combo.addItem(str(i))
            combo.setCurrentIndex(0)
            self.tbl.setCellWidget(r, 2, combo)

        self.lbl_status.setText(
            f"{len(layers)} capa(s) raster encontradas en el proyecto."
        )

    def _check_all(self, state):
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is not None:
                it.setCheckState(state)

    def get_selected(self):
        """Lista de dicts: source_path, source_label, nbands, band_idx, default_name."""
        out = []
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is None or it.checkState() != Qt.Checked:
                continue
            src = it.data(Qt.UserRole) or ""
            lname = it.data(Qt.UserRole + 1) or it.text()
            nb = int(it.data(Qt.UserRole + 2) or 1)
            combo = self.tbl.cellWidget(r, 2)
            band_idx = int(combo.currentText()) if combo else 1
            label = f"{lname} (capa QGIS)"
            default_name = _default_name(src, band_idx, layer_name=lname)
            out.append({
                "source_path": src,
                "source_label": label,
                "nbands": nb,
                "band_idx": band_idx,
                "name": default_name,
            })
        return out


# ---------------------------------------------------------------------------
# Dialogo principal
# ---------------------------------------------------------------------------

class CombinarBandasNombresDialog(QDialog):

    COLS = ("#", "Origen (capa o archivo)", "Banda", "Nombre de la banda")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Combinar bandas con nombres - Geomaticape")
        self.resize(900, 560)
        self._build_ui()

    # ------------------- UI ---------------------------------------------

    def _build_ui(self):
        v = QVBoxLayout(self)

        info = QLabel(
            "<b>Combinar bandas con nombres</b><br>"
            "Agrega <b>archivos del disco</b> o <b>capas raster del "
            "proyecto QGIS</b>. Para cada entrada elige la <b>banda</b> "
            "(combo en la columna <i>Banda</i>) y edita el <b>nombre</b> "
            "directamente al costado en la columna <i>Nombre de la "
            "banda</i> (doble click o F2). El orden de las filas (Subir / "
            "Bajar) define el orden de las bandas en el GeoTIFF de salida."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        # Tabla principal: 4 columnas
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 240)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
            | QAbstractItemView.AnyKeyPressed
        )
        v.addWidget(self.table, 1)

        # Botonera
        row = QHBoxLayout()
        self.btn_add_file = QPushButton("Agregar raster (archivo)...")
        self.btn_add_file.clicked.connect(self._on_add_file)
        self.btn_add_qgis = QPushButton("Agregar capa(s) QGIS...")
        self.btn_add_qgis.clicked.connect(self._on_add_qgis)
        self.btn_remove = QPushButton("Quitar")
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_up = QPushButton("Subir")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = QPushButton("Bajar")
        self.btn_down.clicked.connect(lambda: self._move(+1))
        self.btn_clear = QPushButton("Limpiar")
        self.btn_clear.clicked.connect(self._on_clear)
        for b in (self.btn_add_file, self.btn_add_qgis, self.btn_remove,
                  self.btn_up, self.btn_down, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        # Opciones + salida
        f = QFormLayout()
        self.combo_resample = QComboBox()
        self.combo_resample.addItems(RESAMPLE_METHODS)
        self.combo_resample.setCurrentText("bilinear")
        f.addRow("Metodo de remuestreo:", self.combo_resample)

        self.combo_compress = QComboBox()
        self.combo_compress.addItems(COMPRESS_OPTIONS)
        f.addRow("Compresion GeoTIFF:", self.combo_compress)

        out_widget = QWidget()
        oh = QHBoxLayout(out_widget)
        oh.setContentsMargins(0, 0, 0, 0)
        self.line_out = QLineEdit()
        self.btn_out = QPushButton("...")
        self.btn_out.setFixedWidth(34)
        self.btn_out.clicked.connect(self._on_out)
        oh.addWidget(self.line_out, 1)
        oh.addWidget(self.btn_out)
        f.addRow("Raster de salida (.tif):", out_widget)

        v.addLayout(f)

        # OK/Cancel
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Ejecutar")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # ------------------- helpers de filas --------------------------------

    def _append_row(self, source_path, source_label, nbands, band_idx, name):
        nb = max(1, int(nbands))
        bi = max(1, min(int(band_idx), nb))

        r = self.table.rowCount()
        self.table.insertRow(r)

        # Col 0: #
        it_n = QTableWidgetItem(str(r + 1))
        it_n.setFlags(it_n.flags() & ~Qt.ItemIsEditable)
        it_n.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(r, 0, it_n)

        # Col 1: Origen
        it_p = QTableWidgetItem(source_label)
        it_p.setToolTip(source_path)
        it_p.setData(Qt.UserRole, source_path)
        it_p.setData(Qt.UserRole + 1, nb)
        it_p.setFlags(it_p.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, 1, it_p)

        # Col 2: Banda combo
        combo = QComboBox()
        for i in range(1, nb + 1):
            combo.addItem(str(i))
        combo.setCurrentIndex(bi - 1)
        self.table.setCellWidget(r, 2, combo)

        # Col 3: Nombre editable
        it_name = QTableWidgetItem(name)
        it_name.setToolTip("Doble click para editar el nombre de la banda")
        self.table.setItem(r, 3, it_name)

        # Foco en nombre para edicion rapida
        self.table.setCurrentCell(r, 3)
        try:
            self.table.editItem(it_name)
        except Exception:
            pass

    def _snapshot_rows(self):
        rows = []
        for r in range(self.table.rowCount()):
            it_p = self.table.item(r, 1)
            combo = self.table.cellWidget(r, 2)
            it_n = self.table.item(r, 3)
            if it_p is None:
                continue
            rows.append({
                "source_path":  it_p.data(Qt.UserRole) or "",
                "source_label": it_p.text() or "",
                "nbands":       int(it_p.data(Qt.UserRole + 1) or 1),
                "band_idx":     int(combo.currentText()) if combo else 1,
                "name":         it_n.text() if it_n else "",
            })
        return rows

    def _populate_from_rows(self, rows):
        self.table.setRowCount(0)
        for d in rows:
            self._append_row(
                d["source_path"], d["source_label"],
                d["nbands"], d["band_idx"], d["name"]
            )

    # ------------------- acciones ----------------------------------------

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
                band_idx=1,
                name=_default_name(f, 1),
            )

    def _on_add_qgis(self):
        dlg = _QGISLayerPickerDialog(parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        items = dlg.get_selected()
        if not items:
            QMessageBox.information(
                self, "Combinar bandas",
                "No marcaste ninguna capa."
            )
            return
        for d in items:
            self._append_row(
                source_path=d["source_path"],
                source_label=d["source_label"],
                nbands=d["nbands"],
                band_idx=d["band_idx"],
                name=d["name"],
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

    def _renumber(self):
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is None:
                it = QTableWidgetItem()
                self.table.setItem(r, 0, it)
            it.setText(str(r + 1))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setTextAlignment(Qt.AlignCenter)

    def _on_out(self):
        start = self.line_out.text().strip()
        f, _ = QFileDialog.getSaveFileName(
            self, "Raster combinado de salida", start,
            "GeoTIFF (*.tif *.tiff)"
        )
        if f:
            if not f.lower().endswith((".tif", ".tiff")):
                f += ".tif"
            self.line_out.setText(f)

    # ------------------- ejecutar ----------------------------------------

    def _gather(self):
        rows = self._snapshot_rows()
        paths, bands, names = [], [], []
        for d in rows:
            p = d["source_path"]
            if not p:
                continue
            n = (d["name"] or "").strip()
            if not n:
                n = _default_name(p, d["band_idx"])
            paths.append(p)
            bands.append(int(d["band_idx"]))
            names.append(_safe_name(n))
        return paths, bands, names

    def _on_ok(self):
        # Forzar fin de edicion en curso
        cur = self.table.currentItem()
        if cur is not None:
            try:
                self.table.closePersistentEditor(cur)
            except Exception:
                pass

        if self.table.rowCount() < 2:
            QMessageBox.warning(self, "Combinar bandas",
                                "Agrega al menos 2 raster.")
            return
        out_path = self.line_out.text().strip()
        if not out_path:
            QMessageBox.warning(self, "Combinar bandas",
                                "Define el raster de salida (.tif).")
            return
        if not out_path.lower().endswith((".tif", ".tiff")):
            out_path += ".tif"
            self.line_out.setText(out_path)

        paths, bands, names = self._gather()

        # Aviso por nombres duplicados
        seen = set()
        dup = set()
        for n in names:
            if n in seen:
                dup.add(n)
            seen.add(n)
        if dup:
            r = QMessageBox.question(
                self, "Combinar bandas",
                "Hay nombres de banda duplicados: "
                + ", ".join(sorted(dup))
                + ".\nContinuar de todos modos?",
                QMessageBox.Yes | QMessageBox.No
            )
            if r != QMessageBox.Yes:
                return

        resample = self.combo_resample.currentText()
        compress = self.combo_compress.currentText()

        progress = QProgressDialog("Procesando...", "Cancelar", 0, 100, self)
        progress.setWindowTitle("Combinar bandas con nombres")
        progress.setWindowModality(Qt.WindowModal)
        progress.setAutoClose(True)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        feedback = _DialogFeedback(progress)
        try:
            ejecutar_combinacion(paths, bands, names, out_path,
                                 resample, compress, feedback)
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Combinar bandas - Error", str(e))
            return

        progress.close()

        # Cargar resultado en QGIS si esta disponible
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            lyr = QgsRasterLayer(out_path, os.path.basename(out_path))
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
        except Exception:
            pass

        QMessageBox.information(
            self, "Combinar bandas",
            "Raster combinado generado:\n"
            + out_path
            + "\n\nBandas:\n  "
            + "\n  ".join(
                f"{i+1}. {n}  (banda origen: {b})"
                for i, (n, b) in enumerate(zip(names, bands))
            )
        )
        self.accept()


# ---------------------------------------------------------------------------
# Wrapper invocado desde el menu Geomaticape -> Procesamiento
# ---------------------------------------------------------------------------

class CombinarBandasNombres:
    """Lanzador desde el menu del plugin."""

    def __init__(self, iface=None):
        self.iface = iface

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "combinar_bandas.png"))

    def run(self):
        parent = None
        try:
            from qgis.utils import iface as _qgis_iface
            if _qgis_iface is not None:
                parent = _qgis_iface.mainWindow()
        except Exception:
            parent = None
        dlg = CombinarBandasNombresDialog(parent=parent)
        dlg.exec_()
