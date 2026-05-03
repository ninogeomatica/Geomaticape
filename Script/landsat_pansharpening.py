"""
Landsat Pansharpening 30m -> 15m
=================================
Aumenta la resolucion espacial de las bandas multiespectrales de Landsat
(30 m) a 15 m fusionandolas con la banda pancromatica (15 m). Aplica el
algoritmo "Weighted Brovey" implementado nativamente en GDAL via VRT
pansharpened (subClass="VRTPansharpenedDataset"), que es el metodo de
referencia para Landsat 7 ETM+ y Landsat 8/9 OLI.

UI Qt personalizada (similar a "Combinar bandas con nombres"):
  * Bandas multiespectrales: tabla con # / Origen / Banda / Nombre.
    Se aceptan archivos del disco (botones "Agregar raster (archivo)...")
    y capas raster del proyecto QGIS ("Agregar capa(s) QGIS...").
  * Banda pancromatica: una sola entrada (archivo o capa QGIS) y combo
    para escoger la banda dentro del raster PAN.
  * Salida: GeoTIFF multibanda a la resolucion del PAN (~15 m), con cada
    banda llevando el NOMBRE asignado por el usuario en la tabla
    (band.SetDescription("Blue"), "Green", "Red", "NIR"...).

Notas tecnicas:
  * El raster de salida hereda CRS, GeoTransform y extent del PAN.
  * Si las MS no comparten extent / CRS con el PAN, GDAL las reproyecta
    internamente al construir el VRT pansharpened.
  * Pesos por defecto: uniformes 1/N. El usuario puede ajustarlos en el
    cuadro "Pesos PAN" como lista separada por comas (deben sumar ~1;
    si suman distinto, GDAL los normaliza).
  * Tipos comunes de PAN para Landsat:
      - Landsat 7 ETM+ : banda 8 (15 m) cubre ~Green+Red+NIR.
      - Landsat 8/9 OLI: banda 8 (15 m) cubre ~Blue+Green+Red.

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.15
Grupo : Procesamiento
"""

import os
import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QPushButton, QLabel,
    QLineEdit, QFileDialog, QMessageBox, QComboBox, QDialogButtonBox,
    QProgressDialog, QApplication, QWidget, QAbstractItemView,
)
from osgeo import gdal


# Reusamos utilidades del modulo de Combinar bandas (mismo paquete).
from .combinar_bandas_nombres import (
    _safe_name,
    _default_name,
    _band_count,
    _detect_band_name_at,
    _DialogFeedback,
    _QGISLayerPickerDialog,
    RESAMPLE_METHODS,
    COMPRESS_OPTIONS,
)


# Algoritmos disponibles via VRT pansharpened en GDAL.
PANSHARP_ALGORITHMS = ["WeightedBrovey"]

# Metodo de remuestreo para subir las MS al grid del PAN.
PAN_RESAMPLE = ["Cubic", "CubicSpline", "Bilinear", "Lanczos", "Average"]


# ---------------------------------------------------------------------------
# Construccion del VRT pansharpened
# ---------------------------------------------------------------------------

def _build_pansharpened_vrt(pan_path, pan_band, ms_paths, ms_bands,
                            algorithm, weights, resample,
                            nodata=None, bit_depth=None):
    """Genera el XML de un VRT subClass=VRTPansharpenedDataset.

    Parametros:
        pan_path, pan_band   : ruta del raster pancromatico y banda dentro
        ms_paths, ms_bands   : listas paralelas con MS y banda
        algorithm            : "WeightedBrovey"
        weights              : lista de pesos uno por banda MS (sum ~ 1)
        resample             : metodo de remuestreo para llevar MS a grid PAN
        nodata, bit_depth    : opcionales
    """
    # Tamano del VRT = tamano del PAN.
    pan_ds = gdal.Open(pan_path, gdal.GA_ReadOnly)
    if pan_ds is None:
        raise RuntimeError(f"GDAL no pudo abrir el PAN: {pan_path}")
    xs, ys = pan_ds.RasterXSize, pan_ds.RasterYSize
    pan_ds = None

    # Bloque de pesos (si se proporcionan).
    weights_xml = ""
    if weights:
        weights_xml = (
            "    <AlgorithmOptions>\n"
            f"      <Weights>{','.join(f'{float(w):.6f}' for w in weights)}</Weights>\n"
            "    </AlgorithmOptions>\n"
        )

    # SpectralBand entries (uno por banda MS).
    spectral_xml = ""
    for i, (p, b) in enumerate(zip(ms_paths, ms_bands), start=1):
        # Las rutas pueden tener caracteres especiales: usar relativeToVRT=0
        # y escapar minimamente.
        spectral_xml += (
            f'    <SpectralBand dstBand="{i}">\n'
            f'      <SourceFilename relativeToVRT="0">{_xml_escape(p)}</SourceFilename>\n'
            f'      <SourceBand>{int(b)}</SourceBand>\n'
            f'    </SpectralBand>\n'
        )

    nodata_xml = ""
    if nodata is not None:
        nodata_xml = f"    <NoData>{nodata}</NoData>\n"

    bitdepth_xml = ""
    if bit_depth is not None:
        bitdepth_xml = f"    <BitDepth>{int(bit_depth)}</BitDepth>\n"

    vrt_xml = (
        f'<VRTDataset rasterXSize="{xs}" rasterYSize="{ys}" '
        f'subClass="VRTPansharpenedDataset">\n'
        f'  <PansharpeningOptions>\n'
        f'    <Algorithm>{algorithm}</Algorithm>\n'
        f'{weights_xml}'
        f'    <Resampling>{resample}</Resampling>\n'
        f'    <NumThreads>ALL_CPUS</NumThreads>\n'
        f'{nodata_xml}'
        f'{bitdepth_xml}'
        f'    <PanchroBand>\n'
        f'      <SourceFilename relativeToVRT="0">{_xml_escape(pan_path)}</SourceFilename>\n'
        f'      <SourceBand>{int(pan_band)}</SourceBand>\n'
        f'    </PanchroBand>\n'
        f'{spectral_xml}'
        f'  </PansharpeningOptions>\n'
        f'</VRTDataset>\n'
    )
    return vrt_xml


def _xml_escape(s):
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# Logica principal
# ---------------------------------------------------------------------------

def ejecutar_pansharpening(ms_paths, ms_bands, ms_names,
                           pan_path, pan_band,
                           out_path, algorithm, weights, resample,
                           compress, feedback,
                           nodata=None, bit_depth=None):
    """Genera el GeoTIFF pansharpeneado a la resolucion del PAN."""

    if not ms_paths or len(ms_paths) < 1:
        raise RuntimeError("Agrega al menos una banda multiespectral.")
    if not pan_path:
        raise RuntimeError("Selecciona la banda pancromatica.")
    if not (len(ms_paths) == len(ms_bands) == len(ms_names)):
        raise RuntimeError(
            "Listas inconsistentes (paths/bands/names) en la entrada MS."
        )

    n_ms = len(ms_paths)

    # Pesos por defecto: uniformes 1/N.
    if not weights:
        weights = [1.0 / n_ms] * n_ms
    elif len(weights) != n_ms:
        raise RuntimeError(
            f"La lista de pesos PAN tiene {len(weights)} valores "
            f"pero hay {n_ms} bandas MS."
        )

    # Info diagnostica.
    pan_info = _open_info(pan_path)
    feedback.pushInfo("=" * 64)
    feedback.pushInfo("Landsat Pansharpening 30 m -> 15 m")
    feedback.pushInfo(f"Algoritmo : {algorithm}")
    feedback.pushInfo(f"Remuestreo: {resample}")
    feedback.pushInfo(f"PAN       : {os.path.basename(pan_path)} "
                      f"(banda {pan_band}, {pan_info['cols']}x{pan_info['rows']} px, "
                      f"px ~{abs(pan_info['gt'][1]):.2f} m)")
    feedback.pushInfo("Bandas multiespectrales (orden = orden de salida):")
    for i, (p, b, n, w) in enumerate(zip(ms_paths, ms_bands, ms_names, weights), 1):
        feedback.pushInfo(
            f"  Banda {i:2d}: {n:<14s} (peso {w:.3f})  <-  "
            f"{os.path.basename(p)} (banda origen: {b})"
        )
    feedback.pushInfo("=" * 64)
    feedback.setProgress(5)

    # 1) Construir VRT pansharpened
    out_dir = os.path.dirname(out_path) or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    vrt_path = os.path.splitext(out_path)[0] + "_pansharp.vrt"

    feedback.pushInfo("Construyendo VRT pansharpened...")
    vrt_xml = _build_pansharpened_vrt(
        pan_path=pan_path, pan_band=pan_band,
        ms_paths=ms_paths, ms_bands=ms_bands,
        algorithm=algorithm, weights=weights,
        resample=resample, nodata=nodata, bit_depth=bit_depth,
    )
    with open(vrt_path, "w", encoding="utf-8") as fh:
        fh.write(vrt_xml)

    # Verificar que GDAL pueda abrirlo
    test = gdal.Open(vrt_path, gdal.GA_ReadOnly)
    if test is None:
        raise RuntimeError(
            "GDAL no pudo interpretar el VRT pansharpened. "
            "Revisa que las bandas multiespectrales y la PAN sean validas "
            "y compartan al menos un sistema de referencia."
        )
    test = None
    feedback.setProgress(20)

    # 2) Materializar a GeoTIFF
    feedback.pushInfo("Escribiendo GeoTIFF de salida (puede tardar)...")
    creation = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if compress and compress != "NONE":
        creation.append(f"COMPRESS={compress}")

    def _gdal_progress(pct, message, user_data):
        if feedback.isCanceled():
            return 0  # cancelar
        try:
            feedback.setProgress(20 + int(pct * 70))
        except Exception:
            pass
        return 1

    try:
        gdal.Translate(
            out_path, vrt_path,
            creationOptions=creation,
            callback=_gdal_progress,
        )
    except Exception as e:
        try:
            os.remove(vrt_path)
        except OSError:
            pass
        raise RuntimeError(f"gdal.Translate fallo: {e}")
    feedback.setProgress(92)

    # 3) Escribir nombres de banda en el output
    ds_out = gdal.Open(out_path, gdal.GA_Update)
    if ds_out is None:
        raise RuntimeError(
            f"No se pudo abrir el GeoTIFF de salida para escribir nombres: "
            f"{out_path}"
        )
    for i, nm in enumerate(ms_names, start=1):
        b = ds_out.GetRasterBand(i)
        b.SetDescription(nm)
        try:
            b.SetMetadataItem("BAND_NAME", nm)
        except Exception:
            pass
    try:
        ds_out.SetMetadataItem("GEOMATICAPE_BAND_ORDER", ",".join(ms_names))
        ds_out.SetMetadataItem("GEOMATICAPE_PANSHARP_ALG", algorithm)
        ds_out.SetMetadataItem("GEOMATICAPE_PANSHARP_RESAMPLE", resample)
        ds_out.SetMetadataItem(
            "GEOMATICAPE_PANSHARP_WEIGHTS",
            ",".join(f"{w:.6f}" for w in weights),
        )
    except Exception:
        pass
    ds_out.FlushCache()
    ds_out = None

    # 4) Limpieza del VRT temporal
    try:
        os.remove(vrt_path)
    except OSError:
        pass

    feedback.pushInfo("=" * 64)
    feedback.pushInfo(f"OK - Raster pansharpeneado: {out_path}")
    feedback.pushInfo(f"Bandas: {', '.join(ms_names)}")
    feedback.pushInfo("=" * 64)
    feedback.setProgress(100)
    return out_path


def _open_info(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL no pudo abrir: {path}")
    info = {
        "cols":   ds.RasterXSize,
        "rows":   ds.RasterYSize,
        "gt":     ds.GetGeoTransform(),
        "proj":   ds.GetProjection(),
        "nbands": ds.RasterCount,
    }
    ds = None
    return info


# ---------------------------------------------------------------------------
# Dialogo principal
# ---------------------------------------------------------------------------

class LandsatPansharpeningDialog(QDialog):

    COLS = ("#", "Origen (capa o archivo)", "Banda", "Nombre de la banda")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Landsat Pansharpening 30m -> 15m - Geomaticape")
        self.resize(960, 760)

        # Estado del PAN
        self._pan_path = ""
        self._pan_label = ""
        self._pan_nbands = 1

        self._build_ui()

    # -------------------- UI ---------------------------------------------

    def _build_ui(self):
        v = QVBoxLayout(self)

        info = QLabel(
            "<b>Landsat Pansharpening (Weighted Brovey)</b><br>"
            "Aumenta las bandas multiespectrales de Landsat (30 m) a la "
            "resolucion de la pancromatica (15 m). Cada banda de salida "
            "lleva el <b>nombre asignado</b> en la tabla "
            "(<i>Blue, Green, Red, NIR</i>...). Recomendado para Landsat "
            "7 ETM+ y Landsat 8/9 OLI."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        # ---- Grupo: Bandas multiespectrales ----
        gb_ms = QGroupBox("Bandas multiespectrales (30 m)")
        gb_ms_l = QVBoxLayout(gb_ms)

        self.table = QTableWidget(0, 4, gb_ms)
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
        gb_ms_l.addWidget(self.table, 1)

        row_ms = QHBoxLayout()
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
            row_ms.addWidget(b)
        row_ms.addStretch(1)
        gb_ms_l.addLayout(row_ms)

        v.addWidget(gb_ms, 1)

        # ---- Grupo: Banda PAN ----
        gb_pan = QGroupBox("Banda pancromatica (15 m)")
        gb_pan_l = QFormLayout(gb_pan)

        # Linea con label de PAN + botones para escogerla
        pan_widget = QWidget()
        ph = QHBoxLayout(pan_widget); ph.setContentsMargins(0, 0, 0, 0)
        self.lbl_pan = QLineEdit()
        self.lbl_pan.setReadOnly(True)
        self.lbl_pan.setPlaceholderText("(sin seleccionar)")
        self.btn_pan_qgis = QPushButton("Capa QGIS...")
        self.btn_pan_qgis.clicked.connect(self._on_pan_from_qgis)
        self.btn_pan_file = QPushButton("Archivo...")
        self.btn_pan_file.clicked.connect(self._on_pan_from_file)
        ph.addWidget(self.lbl_pan, 1)
        ph.addWidget(self.btn_pan_qgis)
        ph.addWidget(self.btn_pan_file)
        gb_pan_l.addRow("Origen del PAN:", pan_widget)

        self.combo_pan_band = QComboBox()
        self.combo_pan_band.addItem("1")
        self.combo_pan_band.setMaximumWidth(80)
        gb_pan_l.addRow("Banda dentro del PAN:", self.combo_pan_band)

        v.addWidget(gb_pan)

        # ---- Grupo: Opciones del algoritmo y salida ----
        gb_opts = QGroupBox("Opciones de pansharpening y salida")
        f = QFormLayout(gb_opts)

        self.combo_alg = QComboBox()
        self.combo_alg.addItems(PANSHARP_ALGORITHMS)
        f.addRow("Algoritmo:", self.combo_alg)

        self.combo_resample = QComboBox()
        self.combo_resample.addItems(PAN_RESAMPLE)
        self.combo_resample.setCurrentText("Cubic")
        f.addRow("Remuestreo MS -> grid PAN:", self.combo_resample)

        self.line_weights = QLineEdit()
        self.line_weights.setPlaceholderText(
            "vacio = pesos uniformes 1/N (sumando 1)"
        )
        f.addRow("Pesos PAN (coma):", self.line_weights)

        self.combo_compress = QComboBox()
        self.combo_compress.addItems(COMPRESS_OPTIONS)
        f.addRow("Compresion GeoTIFF:", self.combo_compress)

        out_widget = QWidget()
        oh = QHBoxLayout(out_widget); oh.setContentsMargins(0, 0, 0, 0)
        self.line_out = QLineEdit()
        self.btn_out = QPushButton("...")
        self.btn_out.setFixedWidth(34)
        self.btn_out.clicked.connect(self._on_out)
        oh.addWidget(self.line_out, 1)
        oh.addWidget(self.btn_out)
        f.addRow("Raster de salida (.tif):", out_widget)

        v.addWidget(gb_opts)

        # ---- OK / Cancel ----
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Ejecutar pansharpening")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # -------------------- helpers tabla MS -------------------------------

    def _append_row(self, source_path, source_label, nbands, band_idx, name):
        nb = max(1, int(nbands))
        bi = max(1, min(int(band_idx), nb))
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
        for i in range(1, nb + 1):
            combo.addItem(str(i))
        combo.setCurrentIndex(bi - 1)
        self.table.setCellWidget(r, 2, combo)

        it_name = QTableWidgetItem(name)
        it_name.setToolTip("Doble click para editar el nombre de la banda")
        self.table.setItem(r, 3, it_name)

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

    # -------------------- acciones MS ------------------------------------

    def _on_add_file(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecciona uno o mas raster multiespectrales",
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
            QMessageBox.information(self, "Pansharpening",
                                    "No marcaste ninguna capa.")
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

    # -------------------- acciones PAN -----------------------------------

    def _set_pan(self, source_path, source_label, nbands):
        self._pan_path = source_path
        self._pan_label = source_label
        self._pan_nbands = max(1, int(nbands))
        self.lbl_pan.setText(f"{source_label}   ({self._pan_nbands} banda(s))")
        # Repoblar combo de bandas PAN
        cur = self.combo_pan_band.currentText()
        self.combo_pan_band.clear()
        for i in range(1, self._pan_nbands + 1):
            self.combo_pan_band.addItem(str(i))
        if cur and cur.isdigit() and 1 <= int(cur) <= self._pan_nbands:
            self.combo_pan_band.setCurrentText(cur)
        else:
            self.combo_pan_band.setCurrentIndex(0)

    def _on_pan_from_qgis(self):
        # Reusamos el picker genérico, pero solo permitimos UNA capa.
        dlg = _QGISLayerPickerDialog(parent=self)
        dlg.setWindowTitle("Selecciona la banda pancromatica desde QGIS")
        if dlg.exec_() != QDialog.Accepted:
            return
        items = dlg.get_selected()
        if not items:
            return
        if len(items) > 1:
            QMessageBox.information(
                self, "Pansharpening",
                f"Marcaste {len(items)} capas; se usara la primera "
                f"({items[0]['source_label']}). Si necesitas otra, vuelve a "
                f"abrir el selector y deja solo una marcada."
            )
        d = items[0]
        # El picker permite escoger banda; respetamos esa eleccion como
        # banda activa por defecto del PAN.
        self._set_pan(d["source_path"], d["source_label"], d["nbands"])
        # Si el usuario eligio una banda en el picker, pre-seleccionar.
        if 1 <= int(d["band_idx"]) <= self._pan_nbands:
            self.combo_pan_band.setCurrentText(str(int(d["band_idx"])))

    def _on_pan_from_file(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Selecciona el raster pancromatico",
            "",
            "Raster (*.tif *.tiff *.img *.vrt *.jp2 *.dat);;Todos (*.*)"
        )
        if not f:
            return
        nb = _band_count(f)
        self._set_pan(f, os.path.basename(f), nb)

    # -------------------- salida -----------------------------------------

    def _on_out(self):
        start = self.line_out.text().strip()
        f, _ = QFileDialog.getSaveFileName(
            self, "Raster pansharpeneado de salida", start,
            "GeoTIFF (*.tif *.tiff)"
        )
        if f:
            if not f.lower().endswith((".tif", ".tiff")):
                f += ".tif"
            self.line_out.setText(f)

    # -------------------- ejecutar ---------------------------------------

    def _gather_ms(self):
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

    def _parse_weights(self, text, n):
        text = (text or "").strip()
        if not text:
            return None  # uniformes
        try:
            ws = [float(x.strip()) for x in re.split(r"[,;\s]+", text) if x.strip()]
        except ValueError:
            raise RuntimeError(
                "Pesos PAN invalidos. Usa numeros separados por coma "
                "(ej: 0.25,0.25,0.25,0.25)."
            )
        if len(ws) != n:
            raise RuntimeError(
                f"Diste {len(ws)} pesos pero hay {n} bandas multiespectrales."
            )
        return ws

    def _on_ok(self):
        # Cerrar edicion en curso de la tabla.
        cur = self.table.currentItem()
        if cur is not None:
            try:
                self.table.closePersistentEditor(cur)
            except Exception:
                pass

        if self.table.rowCount() < 1:
            QMessageBox.warning(self, "Pansharpening",
                                "Agrega al menos una banda multiespectral.")
            return
        if not self._pan_path:
            QMessageBox.warning(self, "Pansharpening",
                                "Selecciona la banda pancromatica.")
            return

        out_path = self.line_out.text().strip()
        if not out_path:
            QMessageBox.warning(self, "Pansharpening",
                                "Define el raster de salida (.tif).")
            return
        if not out_path.lower().endswith((".tif", ".tiff")):
            out_path += ".tif"
            self.line_out.setText(out_path)

        ms_paths, ms_bands, ms_names = self._gather_ms()

        # Pesos
        try:
            weights = self._parse_weights(self.line_weights.text(), len(ms_paths))
        except RuntimeError as e:
            QMessageBox.warning(self, "Pansharpening", str(e))
            return

        # Banda PAN
        try:
            pan_band = int(self.combo_pan_band.currentText())
        except (TypeError, ValueError):
            pan_band = 1

        # Aviso por nombres duplicados.
        seen = set()
        dup = set()
        for n in ms_names:
            if n in seen:
                dup.add(n)
            seen.add(n)
        if dup:
            r = QMessageBox.question(
                self, "Pansharpening",
                "Hay nombres de banda duplicados: "
                + ", ".join(sorted(dup))
                + ".\nContinuar de todos modos?",
                QMessageBox.Yes | QMessageBox.No
            )
            if r != QMessageBox.Yes:
                return

        algorithm = self.combo_alg.currentText()
        resample = self.combo_resample.currentText()
        compress = self.combo_compress.currentText()

        progress = QProgressDialog("Procesando...", "Cancelar", 0, 100, self)
        progress.setWindowTitle("Landsat Pansharpening")
        progress.setWindowModality(Qt.WindowModal)
        progress.setAutoClose(True)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        feedback = _DialogFeedback(progress)
        try:
            ejecutar_pansharpening(
                ms_paths=ms_paths, ms_bands=ms_bands, ms_names=ms_names,
                pan_path=self._pan_path, pan_band=pan_band,
                out_path=out_path,
                algorithm=algorithm, weights=weights, resample=resample,
                compress=compress, feedback=feedback,
            )
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Pansharpening - Error", str(e))
            return

        progress.close()

        # Cargar resultado en QGIS si esta disponible.
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            lyr = QgsRasterLayer(out_path, os.path.basename(out_path))
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
        except Exception:
            pass

        QMessageBox.information(
            self, "Pansharpening",
            "Pansharpening generado:\n"
            + out_path
            + "\n\nBandas (15 m):\n  "
            + "\n  ".join(
                f"{i+1}. {n}  (banda origen: {b})"
                for i, (n, b) in enumerate(zip(ms_names, ms_bands))
            )
        )
        self.accept()


# ---------------------------------------------------------------------------
# Wrapper invocado desde el menu Geomaticape -> Procesamiento
# ---------------------------------------------------------------------------

class LandsatPansharpening:
    """Lanzador desde el menu del plugin."""

    def __init__(self, iface=None):
        self.iface = iface

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "landsat.png"))

    def run(self):
        parent = None
        try:
            from qgis.utils import iface as _qgis_iface
            if _qgis_iface is not None:
                parent = _qgis_iface.mainWindow()
        except Exception:
            parent = None
        dlg = LandsatPansharpeningDialog(parent=parent)
        dlg.exec_()
