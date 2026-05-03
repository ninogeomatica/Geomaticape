"""
Extraer valores puntuales de multiples raster
==============================================
Para cada punto de una capa vectorial, extrae los valores de todas las
bandas de uno o varios rasters y produce una capa de puntos (GPKG / SHP)
o una tabla (CSV / XLSX) con los valores muestreados.

Mejoras frente a Point Sampling Tool 0.5.6 (Borys Jurgiel):
  * Reproyeccion AUTOMATICA del punto al CRS de cada raster
    (PST solo advierte y obliga al usuario a reproyectar manualmente).
  * Interpolacion bilineal opcional ademas del vecino mas proximo.
  * Soporte explicito de bandas multiespectrales con seleccion por banda
    via arbol de capas.
  * Exportacion directa a CSV / XLSX (no solo capa vectorial).
  * Implementado tambien como QgsProcessingAlgorithm para usarse en
    modelos y batch.
  * Manejo robusto de NoData -> NULL.

Fuente original / inspiracion:
    Point Sampling Tool, Copyright (C) 2008-2024 Borys Jurgiel
    https://github.com/borysiasty/pointsamplingtool
    Licencia GPL v2+, atribucion conservada.

Autor: Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.9
Grupo : Geoprocesamiento
"""

import os
import csv

from qgis.PyQt.QtCore import QVariant, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QDialog, QFileDialog, QPushButton, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QLabel, QMessageBox, QGroupBox,
    QDialogButtonBox, QApplication, QComboBox, QTreeWidget,
    QTreeWidgetItem, QCheckBox, QListWidget, QAbstractItemView,
    QProgressBar
)

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProcessingException,
    QgsProcessing,
    QgsProject,
    QgsVectorLayer, QgsRasterLayer,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsPointXY, QgsGeometry, QgsFeature, QgsField, QgsFields,
    QgsVectorFileWriter, QgsWkbTypes, QgsRaster
)
from qgis import processing


# =========================================================================
#  Sampling helpers (CRS-aware, nearest + bilinear)
# =========================================================================

def _transformar_punto(p_xy, crs_src, crs_dst, project=None):
    """Reproyecta QgsPointXY a otro CRS si difieren."""
    if crs_src == crs_dst:
        return p_xy
    ctx = (project or QgsProject.instance()).transformContext()
    tr = QgsCoordinateTransform(crs_src, crs_dst, ctx)
    res = tr.transform(p_xy)
    return QgsPointXY(res.x(), res.y())


def _muestrear_nearest(rlayer, point_in_raster_crs, banda):
    """Muestreo por vecino mas proximo. Devuelve None si fuera de raster
    o si es NoData."""
    dp = rlayer.dataProvider()
    res = dp.identify(point_in_raster_crs, QgsRaster.IdentifyFormatValue)
    if not res.isValid():
        return None
    vals = res.results()
    if banda not in vals:
        return None
    v = vals[banda]
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _muestrear_bilinear(rlayer, point_in_raster_crs, banda):
    """
    Interpolacion bilineal usando GDAL.
    Lee los 4 pixeles que rodean al punto y combina con pesos.
    Devuelve None si fuera del raster o si todos son NoData.
    """
    from osgeo import gdal
    src = rlayer.source()
    ds = gdal.Open(src, gdal.GA_ReadOnly)
    if ds is None:
        return None
    if banda < 1 or banda > ds.RasterCount:
        ds = None
        return None

    gt = ds.GetGeoTransform()
    inv = gdal.InvGeoTransform(gt)
    if inv is None:
        ds = None
        return None

    px, py = gdal.ApplyGeoTransform(inv, point_in_raster_crs.x(),
                                    point_in_raster_crs.y())
    # esquina superior izquierda del 2x2 (centro de pixel = px-0.5)
    cx = px - 0.5
    cy = py - 0.5
    x0 = int(cx)
    y0 = int(cy)
    fx = cx - x0
    fy = cy - y0

    cols = ds.RasterXSize
    rows = ds.RasterYSize
    if x0 < 0 or y0 < 0 or x0 + 1 >= cols or y0 + 1 >= rows:
        # fuera del area cubierta -> caer a nearest
        ds = None
        return None

    band_obj = ds.GetRasterBand(banda)
    nodata = band_obj.GetNoDataValue()

    arr = band_obj.ReadAsArray(x0, y0, 2, 2)
    ds = None
    if arr is None:
        return None

    import math
    vals = []
    weights = []
    w_array = [
        ((1 - fx) * (1 - fy), arr[0, 0]),
        (fx       * (1 - fy), arr[0, 1]),
        ((1 - fx) * fy,       arr[1, 0]),
        (fx       * fy,       arr[1, 1]),
    ]
    total_w = 0.0
    total_v = 0.0
    for w, v in w_array:
        if v is None:
            continue
        try:
            vf = float(v)
        except (TypeError, ValueError):
            continue
        if nodata is not None and vf == nodata:
            continue
        if not math.isfinite(vf):
            continue
        total_v += w * vf
        total_w += w
    if total_w <= 0:
        return None
    return total_v / total_w


# =========================================================================
#  Dialogo personalizado (estilo PST mejorado)
# =========================================================================

class ExtraerValoresPuntualesDialog(QDialog):
    """
    Dialogo:
      1) Capa de puntos
      2) Arbol de rasters con checkboxes por banda (multiselect)
      3) Metodo de interpolacion
      4) Opciones (incluir XY, copiar atributos del punto, agregar al mapa)
      5) Salida: GPKG / SHP / CSV / XLSX
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Extraer valores puntuales de multiples raster")
        self.setMinimumWidth(680)
        self.setMinimumHeight(560)
        self._build_ui()
        self._poblar_capas()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ---- 1. Puntos ----
        gb_p = QGroupBox("1. Capa de puntos (sampling)")
        h1 = QHBoxLayout(gb_p)
        self.cb_points = QComboBox()
        self.cb_points.currentIndexChanged.connect(self._actualizar_atributos)
        h1.addWidget(QLabel("Puntos:"))
        h1.addWidget(self.cb_points, 1)
        layout.addWidget(gb_p)

        # ---- 2. Rasters / bandas ----
        gb_r = QGroupBox("2. Rasters disponibles (marca los que se muestrearan, banda por banda)")
        v_r = QVBoxLayout(gb_r)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Raster / Banda", "Nombre del campo de salida"])
        self.tree.setColumnWidth(0, 350)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        v_r.addWidget(self.tree)
        h_btns_r = QHBoxLayout()
        bt_all = QPushButton("Marcar todas las bandas")
        bt_none = QPushButton("Desmarcar todas")
        bt_all.clicked.connect(lambda: self._toggle_all(True))
        bt_none.clicked.connect(lambda: self._toggle_all(False))
        h_btns_r.addWidget(bt_all)
        h_btns_r.addWidget(bt_none)
        h_btns_r.addStretch(1)
        v_r.addLayout(h_btns_r)
        layout.addWidget(gb_r, 1)

        # ---- 3. Opciones ----
        gb_o = QGroupBox("3. Opciones de muestreo")
        f_o = QFormLayout(gb_o)
        self.cb_interp = QComboBox()
        self.cb_interp.addItems([
            "Vecino mas proximo (nearest)",
            "Bilineal (4 pixeles vecinos)"
        ])
        f_o.addRow("Interpolacion:", self.cb_interp)

        self.chk_xy = QCheckBox("Incluir coordenadas X / Y del punto en la salida")
        self.chk_xy.setChecked(True)
        f_o.addRow(self.chk_xy)

        self.chk_copy_attr = QCheckBox("Copiar atributos de la capa de puntos al resultado")
        self.chk_copy_attr.setChecked(True)
        f_o.addRow(self.chk_copy_attr)
        layout.addWidget(gb_o)

        # ---- 4. Salida ----
        gb_s = QGroupBox("4. Archivo de salida")
        h_s = QHBoxLayout(gb_s)
        self.le_out = QLineEdit()
        bt_out = QPushButton("Examinar...")
        bt_out.clicked.connect(self._pick_out)
        h_s.addWidget(self.le_out, 1)
        h_s.addWidget(bt_out)
        layout.addWidget(gb_s)

        self.chk_load = QCheckBox("Agregar resultado al proyecto (solo para GPKG/SHP)")
        self.chk_load.setChecked(True)
        layout.addWidget(self.chk_load)

        # ---- 5. Botones / progreso ----
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Ejecutar")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # ---- helpers ----
    def _poblar_capas(self):
        self.cb_points.clear()
        self.tree.clear()
        proyecto = QgsProject.instance()
        for lyr_id, lyr in proyecto.mapLayers().items():
            if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == QgsWkbTypes.PointGeometry:
                self.cb_points.addItem(lyr.name(), lyr_id)
            elif isinstance(lyr, QgsRasterLayer):
                top = QTreeWidgetItem(self.tree, [lyr.name(), ""])
                top.setData(0, Qt.UserRole, lyr_id)
                for b in range(1, lyr.bandCount() + 1):
                    bname = lyr.bandName(b) or f"Banda {b}"
                    if lyr.bandCount() == 1:
                        out_name = lyr.name()
                    else:
                        out_name = f"{lyr.name()}_b{b}"
                    child = QTreeWidgetItem(top, [bname, out_name])
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
                    child.setCheckState(0, Qt.Unchecked)
                    child.setData(0, Qt.UserRole, b)  # numero de banda
                top.setExpanded(True)
        self._actualizar_atributos()

    def _actualizar_atributos(self):
        # nada visual por ahora; reservado para mostrar campos
        pass

    def _toggle_all(self, marcar):
        st = Qt.Checked if marcar else Qt.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                top.child(j).setCheckState(0, st)

    def _pick_out(self):
        ruta, _ = QFileDialog.getSaveFileName(
            self, "Archivo de salida", "",
            "GeoPackage (*.gpkg);;Shapefile (*.shp);;CSV (*.csv);;Excel (*.xlsx)"
        )
        if ruta:
            self.le_out.setText(ruta)

    # ---- collector ----
    def get_values(self):
        # raster id -> [bandas a muestrear]
        rasters = {}
        nombres = {}  # (raster_id, banda) -> nombre_campo_salida
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            rid = top.data(0, Qt.UserRole)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.checkState(0) == Qt.Checked:
                    banda = child.data(0, Qt.UserRole)
                    rasters.setdefault(rid, []).append(banda)
                    nombres[(rid, banda)] = child.text(1).strip() or f"r_{rid}_b{banda}"
        return {
            "puntos_id": self.cb_points.currentData(),
            "rasters": rasters,
            "nombres": nombres,
            "interp": self.cb_interp.currentIndex(),  # 0 nearest, 1 bilinear
            "incluir_xy": self.chk_xy.isChecked(),
            "copiar_attr": self.chk_copy_attr.isChecked(),
            "salida": self.le_out.text().strip(),
            "cargar": self.chk_load.isChecked(),
        }


# =========================================================================
#  Funcion central (compartida por dialogo y por QgsProcessingAlgorithm)
# =========================================================================

def _ejecutar_extraccion(point_layer, raster_specs, interp_idx,
                         incluir_xy, copiar_attr, ruta_salida,
                         feedback=None, log=print):
    """
    raster_specs: lista de dicts:
        {"layer": QgsRasterLayer,
         "bandas": [int, ...],
         "nombres": {banda: nombre_campo}}
    interp_idx: 0 = nearest, 1 = bilinear
    Devuelve la ruta efectiva escrita.
    """
    if point_layer is None:
        raise QgsProcessingException("No se especifico capa de puntos.")
    if not raster_specs:
        raise QgsProcessingException("No se especificaron rasters/bandas.")
    if not ruta_salida:
        raise QgsProcessingException("Falta la ruta del archivo de salida.")

    crs_pts = point_layer.crs()
    project = QgsProject.instance()

    # Construir esquema de campos de salida
    fields = QgsFields()
    fid_idx = fields.append(QgsField("fid_pt", QVariant.LongLong))
    if incluir_xy:
        fields.append(QgsField("x_pt", QVariant.Double, len=20, prec=6))
        fields.append(QgsField("y_pt", QVariant.Double, len=20, prec=6))
    src_field_names = []
    if copiar_attr:
        for f in point_layer.fields():
            # evitar colision con fid_pt/x_pt/y_pt
            nm = f.name()
            if nm in ("fid_pt", "x_pt", "y_pt"):
                nm = nm + "_src"
            new_f = QgsField(f)
            new_f.setName(nm)
            fields.append(new_f)
            src_field_names.append(nm)
    raster_field_names = []
    for spec in raster_specs:
        for b in spec["bandas"]:
            nm = spec["nombres"].get(b) or f"{spec['layer'].name()}_b{b}"
            # truncar para shapefile (10 chars)
            if ruta_salida.lower().endswith(".shp") and len(nm) > 10:
                nm = nm[:10]
            fields.append(QgsField(nm, QVariant.Double, "double", 20, 6))
            raster_field_names.append((spec["layer"], b, nm))

    # Crear capa temporal en memoria
    crs_authid = crs_pts.authid() or "EPSG:4326"
    geom_type = "Point"
    mem = QgsVectorLayer(f"{geom_type}?crs={crs_authid}", "extraido_tmp", "memory")
    mem_pr = mem.dataProvider()
    mem_pr.addAttributes(fields)
    mem.updateFields()

    total = point_layer.featureCount()
    log(f"Capa puntos     : {point_layer.name()} ({total} puntos, CRS {crs_authid})")
    log(f"Rasters a usar  : {len(raster_specs)}")
    for spec in raster_specs:
        log(f"  - {spec['layer'].name()} ({spec['layer'].crs().authid()}), bandas {spec['bandas']}")

    # precomputar transformaciones por raster
    transforms = []
    for spec in raster_specs:
        crs_r = spec["layer"].crs()
        transforms.append(crs_r)

    nuevos = []
    for i, feat in enumerate(point_layer.getFeatures()):
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        # multipoint -> single point
        try:
            geom_s = QgsGeometry(geom)
            geom_s.convertToSingleType()
        except Exception:
            geom_s = geom
        try:
            p = geom_s.asPoint()
        except Exception:
            continue

        f_out = QgsFeature(fields)
        attrs = [feat.id()]
        if incluir_xy:
            attrs += [float(p.x()), float(p.y())]
        if copiar_attr:
            src_attrs = feat.attributes()
            attrs += list(src_attrs)

        # muestreo por raster x banda
        for spec, crs_r in zip(raster_specs, transforms):
            p_r = _transformar_punto(p, crs_pts, crs_r, project)
            for b in spec["bandas"]:
                if interp_idx == 1:
                    val = _muestrear_bilinear(spec["layer"], p_r, b)
                    if val is None:
                        # fallback a nearest si bilineal cae fuera
                        val = _muestrear_nearest(spec["layer"], p_r, b)
                else:
                    val = _muestrear_nearest(spec["layer"], p_r, b)
                attrs.append(val if val is not None else None)

        f_out.setAttributes(attrs)
        f_out.setGeometry(QgsGeometry.fromPointXY(p))
        nuevos.append(f_out)

        if feedback is not None:
            if feedback.isCanceled():
                break
            feedback.setProgress(int((i + 1) * 100 / max(total, 1)))

    mem_pr.addFeatures(nuevos)
    mem.updateExtents()

    # Escribir al destino segun extension
    ext = os.path.splitext(ruta_salida)[1].lower()
    if ext == ".csv":
        _exportar_csv(mem, ruta_salida)
    elif ext == ".xlsx":
        _exportar_xlsx(mem, ruta_salida)
    else:
        if ext not in (".gpkg", ".shp"):
            ruta_salida = os.path.splitext(ruta_salida)[0] + ".gpkg"
            ext = ".gpkg"
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.fileEncoding = "UTF-8"
        if ext == ".shp":
            opts.driverName = "ESRI Shapefile"
        else:
            opts.driverName = "GPKG"
        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            mem, ruta_salida, project.transformContext(), opts
        )
        if isinstance(err, tuple) and err[0] != QgsVectorFileWriter.NoError:
            raise QgsProcessingException(
                f"Error escribiendo {ruta_salida}: {err[1]}"
            )

    log(f"OK - {len(nuevos)} puntos muestreados -> {ruta_salida}")
    return ruta_salida


def _exportar_csv(layer, ruta):
    cols = [f.name() for f in layer.fields()]
    cols_full = ["wkt"] + cols
    with open(ruta, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols_full)
        for feat in layer.getFeatures():
            row = [feat.geometry().asWkt() if feat.geometry() else ""]
            row += list(feat.attributes())
            w.writerow(row)


def _exportar_xlsx(layer, ruta):
    try:
        from openpyxl import Workbook
    except ImportError:
        raise QgsProcessingException(
            "Para exportar a Excel necesitas openpyxl:\n"
            "  python -m pip install openpyxl"
        )
    cols = [f.name() for f in layer.fields()]
    wb = Workbook()
    ws = wb.active
    ws.title = "valores_puntuales"
    ws.append(["x", "y"] + cols)
    for feat in layer.getFeatures():
        if feat.geometry():
            p = feat.geometry().asPoint()
            xy = [float(p.x()), float(p.y())]
        else:
            xy = ["", ""]
        ws.append(xy + list(feat.attributes()))
    wb.save(ruta)


# =========================================================================
#  QgsProcessingAlgorithm (uso desde la caja de procesos)
# =========================================================================

class ExtraerValoresPuntuales(QgsProcessingAlgorithm):

    INPUT_POINTS = "INPUT_POINTS"
    INPUT_RASTERS = "INPUT_RASTERS"
    INTERP = "INTERP"
    INCLUDE_XY = "INCLUDE_XY"
    COPY_ATTR = "COPY_ATTR"
    OUTPUT = "OUTPUT"

    def name(self):
        return "extraer_valores_puntuales"

    def displayName(self):
        return "Extraer valores puntuales de multiples raster"

    def group(self):
        return "Geoprocesamiento"

    def groupId(self):
        return "geomaticape_geoprocesamiento"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "extraer_valores.png"))

    def createInstance(self):
        return ExtraerValoresPuntuales()

    def shortHelpString(self):
        return """
<h3>Extraer valores puntuales de multiples raster</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.9<br><br>

<b>Descripcion:</b><br>
Para cada punto de la capa de entrada, extrae los valores de TODAS las
bandas de los rasters seleccionados y produce una capa de puntos
(GPKG/SHP) o una tabla (CSV/XLSX). En el menu del plugin se ofrece un
arbol con seleccion banda a banda.

<b>Mejoras frente a Point Sampling Tool 0.5.6:</b>
<ul>
<li>Reproyecta automaticamente el punto al CRS de cada raster.</li>
<li>Interpolacion <i>bilineal</i> opcional (PST solo nearest).</li>
<li>Exporta a <i>CSV</i> y <i>XLSX</i> ademas de GPKG/SHP.</li>
<li>Procesable en modelos / batch.</li>
</ul>

<b>Fuente / inspiracion:</b><br>
Point Sampling Tool, Borys Jurgiel - GPL v2+<br>
https://github.com/borysiasty/pointsamplingtool

<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT_POINTS, "Capa de puntos",
            types=[QgsProcessing.TypeVectorPoint]
        ))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_RASTERS, "Rasters a muestrear (todas las bandas)",
            layerType=QgsProcessing.TypeRaster
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.INTERP, "Interpolacion",
            options=["Vecino mas proximo", "Bilineal"],
            defaultValue=0, allowMultiple=False
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_XY, "Incluir coordenadas X / Y del punto",
            defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.COPY_ATTR, "Copiar atributos de la capa de puntos",
            defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, "Archivo de salida",
            fileFilter="GeoPackage (*.gpkg);;Shapefile (*.shp);;CSV (*.csv);;Excel (*.xlsx)"
        ))

    def processAlgorithm(self, parameters, context, feedback):
        src = self.parameterAsSource(parameters, self.INPUT_POINTS, context)
        if src is None:
            raise QgsProcessingException("No se pudo cargar la capa de puntos.")
        rasters = self.parameterAsLayerList(parameters, self.INPUT_RASTERS, context)
        if not rasters:
            raise QgsProcessingException("Selecciona al menos un raster.")
        interp = self.parameterAsEnum(parameters, self.INTERP, context)
        inc_xy = self.parameterAsBoolean(parameters, self.INCLUDE_XY, context)
        cp_at = self.parameterAsBoolean(parameters, self.COPY_ATTR, context)
        out_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        # convertir el feature source a una capa para iterar (mantenemos el
        # objeto de la capa original para CRS y atributos)
        # En modo Processing, parameterAsVectorLayer puede devolver el layer
        try:
            point_layer = self.parameterAsVectorLayer(parameters, self.INPUT_POINTS, context)
        except Exception:
            point_layer = None
        if point_layer is None:
            # construir una capa memory con el contenido de src
            point_layer = QgsVectorLayer(
                f"Point?crs={src.sourceCrs().authid()}", "puntos_tmp", "memory"
            )
            point_layer.dataProvider().addAttributes(src.fields())
            point_layer.updateFields()
            point_layer.dataProvider().addFeatures(list(src.getFeatures()))
            point_layer.updateExtents()

        raster_specs = []
        for r in rasters:
            if not isinstance(r, QgsRasterLayer):
                continue
            bandas = list(range(1, r.bandCount() + 1))
            nombres = {b: (f"{r.name()}_b{b}" if r.bandCount() > 1 else r.name())
                       for b in bandas}
            raster_specs.append({"layer": r, "bandas": bandas, "nombres": nombres})

        feedback.pushInfo("====================================================")
        feedback.pushInfo("Extraer valores puntuales de multiples raster")
        feedback.pushInfo(f"Interpolacion: {'Bilineal' if interp == 1 else 'Nearest'}")
        feedback.pushInfo("====================================================")

        ruta = _ejecutar_extraccion(
            point_layer, raster_specs, interp,
            inc_xy, cp_at, out_path,
            feedback=feedback, log=feedback.pushInfo
        )
        return {self.OUTPUT: ruta}

    # ---------- abre el dialogo customizado desde el menu ----------
    def run(self):
        try:
            from qgis.utils import iface
            parent = iface.mainWindow() if iface else None
        except Exception:
            iface = None
            parent = None

        dlg = ExtraerValoresPuntualesDialog(parent)
        if dlg.exec_() != QDialog.Accepted:
            return

        v = dlg.get_values()
        if not v["puntos_id"]:
            QMessageBox.warning(parent, "Aviso", "Selecciona la capa de puntos.")
            return
        if not v["rasters"]:
            QMessageBox.warning(parent, "Aviso", "Marca al menos una banda raster.")
            return
        if not v["salida"]:
            QMessageBox.warning(parent, "Aviso", "Define el archivo de salida.")
            return

        proyecto = QgsProject.instance()
        point_layer = proyecto.mapLayer(v["puntos_id"])
        raster_specs = []
        for rid, bandas in v["rasters"].items():
            r = proyecto.mapLayer(rid)
            if r is None:
                continue
            nombres = {b: v["nombres"].get((rid, b), f"{r.name()}_b{b}")
                       for b in bandas}
            raster_specs.append({"layer": r, "bandas": bandas, "nombres": nombres})

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            ruta = _ejecutar_extraccion(
                point_layer, raster_specs, v["interp"],
                v["incluir_xy"], v["copiar_attr"], v["salida"],
                feedback=None, log=lambda m: None
            )
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(parent, "Error", str(e))
            return
        QApplication.restoreOverrideCursor()

        ext = os.path.splitext(ruta)[1].lower()
        if v["cargar"] and ext in (".gpkg", ".shp"):
            try:
                if iface:
                    iface.addVectorLayer(ruta, os.path.basename(ruta), "ogr")
            except Exception:
                pass

        QMessageBox.information(
            parent, "Geomaticape",
            f"Extraccion completada.\n\n{ruta}"
        )
