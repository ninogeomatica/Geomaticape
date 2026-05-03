"""
Crear poligonos a partir de tabla (CSV / TXT / XLS / XLSX)
==========================================================
Genera tres capas vectoriales (vertices, segmentos, poligonos) a partir
de una tabla con coordenadas X / Y, un campo de agrupamiento (parcela)
y un campo de orden (secuencia de vertices).

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.8
Grupo : Geoprocesamiento
"""

import os
import math

from qgis.PyQt.QtCore import QVariant, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QDialog, QFileDialog, QComboBox, QPushButton,
    QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QLabel, QMessageBox, QGroupBox, QDialogButtonBox,
    QApplication
)

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterCrs,
    QgsProcessingParameterVectorDestination,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsFields, QgsField, QgsFeature, QgsGeometry,
    QgsPointXY, QgsVectorLayer, QgsVectorFileWriter,
    QgsWkbTypes, QgsProject
)
from qgis.gui import QgsProjectionSelectionWidget
from qgis import processing


# ---------------------------------------------------------------
# Funciones utilitarias para leer la tabla y calcular geometria
# ---------------------------------------------------------------

def _leer_tabla(ruta, hoja=0, separador=","):
    """
    Lee una tabla CSV/TXT/XLS/XLSX usando pandas.
    Devuelve (lista_de_filas_dict, lista_de_columnas).
    """
    try:
        import pandas as pd
    except ImportError:
        raise QgsProcessingException(
            "Falta el modulo 'pandas'. Instalalo con:\n"
            "  python -m pip install pandas openpyxl"
        )

    ext = os.path.splitext(ruta)[1].lower()
    if ext in (".xlsx", ".xls"):
        try:
            # hoja puede ser numero ('0') o nombre
            try:
                hoja_arg = int(hoja)
            except (TypeError, ValueError):
                hoja_arg = hoja if hoja else 0
            df = pd.read_excel(ruta, sheet_name=hoja_arg)
        except ImportError:
            raise QgsProcessingException(
                "Para leer .xlsx instala 'openpyxl':\n"
                "  python -m pip install openpyxl"
            )
    elif ext in (".csv", ".txt"):
        sep = separador if separador else ","
        # Sniff delimitador si el usuario deja el default ","
        df = pd.read_csv(ruta, sep=sep, engine="python")
    else:
        raise QgsProcessingException(
            "Formato no soportado. Usa CSV, TXT, XLS o XLSX."
        )

    columnas = [str(c) for c in df.columns]
    # convertir a lista de dicts (mantener tipos basicos)
    filas = df.to_dict(orient="records")
    return filas, columnas


def _calcular_azimut(x1, y1, x2, y2):
    """Azimut topografico (0=N, 90=E) en grados decimales."""
    dx = x2 - x1
    dy = y2 - y1
    az = math.degrees(math.atan2(dx, dy))
    if az < 0:
        az += 360.0
    return az


def _distancia(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)


def _area_poligono(coords):
    """Area de poligono cerrado por formula del calzado (Shoelace)."""
    n = len(coords)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        s += (x1 * y2) - (x2 * y1)
    return abs(s) / 2.0


def _perimetro_poligono(coords):
    n = len(coords)
    if n < 2:
        return 0.0
    p = 0.0
    for i in range(n - 1):
        p += _distancia(coords[i][0], coords[i][1],
                        coords[i + 1][0], coords[i + 1][1])
    # cerrar si no esta cerrado
    if coords[0] != coords[-1]:
        p += _distancia(coords[-1][0], coords[-1][1],
                        coords[0][0], coords[0][1])
    return p


# ---------------------------------------------------------------
# Generacion de las tres capas (vertices, segmentos, poligonos)
# ---------------------------------------------------------------

def _construir_capas(filas, campo_grupo, campo_orden, campo_x, campo_y, crs):
    """
    Construye in-memory tres QgsVectorLayer:
      vertices   (Point)   -> Agrupamiento, Orden, Vertice, Este_X, Norte_Y
      segmentos  (Line)    -> Agrupamiento, Desde, Hasta, Distancia_m, Azimut
      poligonos  (Polygon) -> Agrupamiento, Area_ha, Perimetro_m
    """
    if not filas:
        raise QgsProcessingException("La tabla esta vacia.")

    crs_authid = crs.authid() if crs and crs.isValid() else "EPSG:4326"

    # Validar columnas
    primer = filas[0]
    for col in (campo_grupo, campo_orden, campo_x, campo_y):
        if col not in primer:
            raise QgsProcessingException(
                f"La columna '{col}' no existe en la tabla. "
                f"Columnas disponibles: {list(primer.keys())}"
            )

    # ------- VERTICES (puntos) -------
    fields_v = QgsFields()
    fields_v.append(QgsField("Agrupamiento", QVariant.String, len=80))
    fields_v.append(QgsField("Orden", QVariant.Int))
    fields_v.append(QgsField("Vertice", QVariant.String, len=20))
    fields_v.append(QgsField("Este_X", QVariant.Double, len=20, prec=6))
    fields_v.append(QgsField("Norte_Y", QVariant.Double, len=20, prec=6))

    lyr_vert = QgsVectorLayer(f"Point?crs={crs_authid}", "vertices_tmp", "memory")
    pr_v = lyr_vert.dataProvider()
    pr_v.addAttributes(fields_v)
    lyr_vert.updateFields()

    # agrupar por (grupo, orden) para vertices y para construir poligonos
    grupos = {}
    for fila in filas:
        g = fila[campo_grupo]
        try:
            orden = int(fila[campo_orden])
        except (TypeError, ValueError):
            raise QgsProcessingException(
                f"El campo de orden '{campo_orden}' debe ser numerico entero."
            )
        try:
            x = float(fila[campo_x])
            y = float(fila[campo_y])
        except (TypeError, ValueError):
            raise QgsProcessingException(
                f"Coordenadas no numericas en la fila: {fila}"
            )
        grupos.setdefault(g, []).append((orden, x, y))

    # ordenar cada grupo por orden
    for g in grupos:
        grupos[g].sort(key=lambda r: r[0])

    feats_v = []
    for g, lista in grupos.items():
        for orden, x, y in lista:
            f = QgsFeature(fields_v)
            f.setAttributes([str(g), int(orden), f"V{int(orden)}", float(x), float(y)])
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            feats_v.append(f)
    pr_v.addFeatures(feats_v)
    lyr_vert.updateExtents()

    # ------- SEGMENTOS (lineas) -------
    fields_s = QgsFields()
    fields_s.append(QgsField("Agrupamiento", QVariant.String, len=80))
    fields_s.append(QgsField("Desde", QVariant.String, len=20))
    fields_s.append(QgsField("Hasta", QVariant.String, len=20))
    fields_s.append(QgsField("Distancia_m", QVariant.Double, len=20, prec=4))
    fields_s.append(QgsField("Azimut", QVariant.Double, len=20, prec=4))

    lyr_seg = QgsVectorLayer(f"LineString?crs={crs_authid}", "segmentos_tmp", "memory")
    pr_s = lyr_seg.dataProvider()
    pr_s.addAttributes(fields_s)
    lyr_seg.updateFields()

    feats_s = []
    for g, lista in grupos.items():
        n = len(lista)
        if n < 2:
            continue
        for i in range(n):
            o1, x1, y1 = lista[i]
            o2, x2, y2 = lista[(i + 1) % n]  # cerrar al primero
            dist = _distancia(x1, y1, x2, y2)
            az = _calcular_azimut(x1, y1, x2, y2)
            f = QgsFeature(fields_s)
            f.setAttributes([str(g), f"V{int(o1)}", f"V{int(o2)}",
                             round(dist, 4), round(az, 4)])
            f.setGeometry(QgsGeometry.fromPolylineXY(
                [QgsPointXY(x1, y1), QgsPointXY(x2, y2)]
            ))
            feats_s.append(f)
    pr_s.addFeatures(feats_s)
    lyr_seg.updateExtents()

    # ------- POLIGONOS -------
    fields_p = QgsFields()
    fields_p.append(QgsField("Agrupamiento", QVariant.String, len=80))
    fields_p.append(QgsField("Area_ha", QVariant.Double, len=20, prec=6))
    fields_p.append(QgsField("Perimetro_m", QVariant.Double, len=20, prec=4))

    lyr_pol = QgsVectorLayer(f"Polygon?crs={crs_authid}", "poligonos_tmp", "memory")
    pr_p = lyr_pol.dataProvider()
    pr_p.addAttributes(fields_p)
    lyr_pol.updateFields()

    feats_p = []
    for g, lista in grupos.items():
        if len(lista) < 3:
            continue
        coords = [(x, y) for _, x, y in lista]
        if coords[0] != coords[-1]:
            coords_cerr = coords + [coords[0]]
        else:
            coords_cerr = coords[:]
        area_m2 = _area_poligono(coords)  # sin duplicar el ultimo
        perim = _perimetro_poligono(coords)
        ring = [QgsPointXY(x, y) for x, y in coords_cerr]
        geom = QgsGeometry.fromPolygonXY([ring])
        f = QgsFeature(fields_p)
        f.setAttributes([str(g), round(area_m2 / 10000.0, 6), round(perim, 4)])
        f.setGeometry(geom)
        feats_p.append(f)
    pr_p.addFeatures(feats_p)
    lyr_pol.updateExtents()

    return lyr_vert, lyr_seg, lyr_pol


def _guardar(layer, ruta_destino):
    """
    Guarda un QgsVectorLayer al destino solicitado por la herramienta
    (Shapefile, GeoPackage, etc.). Devuelve la ruta efectiva escrita.
    """
    if not ruta_destino:
        return None
    # Detectar driver por extension
    ext = os.path.splitext(ruta_destino)[1].lower()
    if ext == ".gpkg":
        driver = "GPKG"
    elif ext == ".geojson":
        driver = "GeoJSON"
    else:
        driver = "ESRI Shapefile"

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = driver
    options.fileEncoding = "UTF-8"

    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, ruta_destino,
        QgsProject.instance().transformContext(),
        options
    )
    # writeAsVectorFormatV3 devuelve (errorCode, errorMessage)
    if isinstance(err, tuple):
        if err[0] != QgsVectorFileWriter.NoError:
            raise QgsProcessingException(
                f"No se pudo escribir {ruta_destino}: {err[1]}"
            )
    return ruta_destino


# ---------------------------------------------------------------
# DIALOGO PERSONALIZADO (con combos auto-poblados al cargar tabla)
# ---------------------------------------------------------------

class CrearPoligonosTablaDialog(QDialog):
    """
    Dialogo customizado para la herramienta. Permite:
      1. Elegir archivo (CSV / TXT / XLS / XLSX)
      2. Lee las cabeceras y rellena 4 ComboBox
         (Agrupamiento, Orden, X, Y)
      3. Escoger el CRS de salida
      4. Definir carpeta de salida
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crear poligonos a partir de tabla")
        self.setMinimumWidth(560)
        self._columnas = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ---- archivo ----
        gb_in = QGroupBox("1. Tabla de entrada (CSV / TXT / XLS / XLSX)")
        f_in = QFormLayout(gb_in)

        self.le_file = QLineEdit()
        self.le_file.setReadOnly(True)
        bt_file = QPushButton("Examinar...")
        bt_file.clicked.connect(self._pick_file)
        h1 = QHBoxLayout()
        h1.addWidget(self.le_file)
        h1.addWidget(bt_file)
        f_in.addRow("Archivo:", h1)

        self.le_sep = QLineEdit(",")
        self.le_sep.setMaximumWidth(60)
        f_in.addRow("Separador (CSV/TXT):", self.le_sep)

        self.le_hoja = QLineEdit("0")
        self.le_hoja.setMaximumWidth(120)
        f_in.addRow("Hoja Excel (nombre o indice 0..n):", self.le_hoja)

        bt_load = QPushButton("Cargar columnas de la tabla")
        bt_load.clicked.connect(self._cargar_columnas)
        f_in.addRow(bt_load)
        layout.addWidget(gb_in)

        # ---- campos ----
        gb_f = QGroupBox("2. Campos de la tabla")
        f_f = QFormLayout(gb_f)

        self.cb_grupo = QComboBox()
        self.cb_orden = QComboBox()
        self.cb_x = QComboBox()
        self.cb_y = QComboBox()
        for c in (self.cb_grupo, self.cb_orden, self.cb_x, self.cb_y):
            c.setEditable(True)

        f_f.addRow("Agrupamiento (Parcela):", self.cb_grupo)
        f_f.addRow("Orden (vertices):", self.cb_orden)
        f_f.addRow("Coordenada X (Este):", self.cb_x)
        f_f.addRow("Coordenada Y (Norte):", self.cb_y)
        layout.addWidget(gb_f)

        # ---- CRS ----
        gb_crs = QGroupBox("3. Sistema de coordenadas (CRS) de salida")
        f_crs = QVBoxLayout(gb_crs)
        self.crs_widget = QgsProjectionSelectionWidget()
        # default: proyectado UTM 18S (Peru) si esta disponible
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem("EPSG:32718"))
        f_crs.addWidget(self.crs_widget)
        layout.addWidget(gb_crs)

        # ---- carpeta salida ----
        gb_out = QGroupBox("4. Carpeta de salida (Shapefiles vertices/segmentos/poligonos)")
        f_out = QHBoxLayout(gb_out)
        self.le_out = QLineEdit()
        bt_out = QPushButton("Examinar...")
        bt_out.clicked.connect(self._pick_out)
        f_out.addWidget(self.le_out)
        f_out.addWidget(bt_out)
        layout.addWidget(gb_out)

        # ---- opcion: cargar al proyecto ----
        from qgis.PyQt.QtWidgets import QCheckBox
        self.chk_load = QCheckBox("Cargar capas al proyecto al finalizar")
        self.chk_load.setChecked(True)
        layout.addWidget(self.chk_load)

        # ---- botones ----
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # --- callbacks ---
    def _pick_file(self):
        ruta, _ = QFileDialog.getOpenFileName(
            self, "Selecciona tabla", "",
            "Tablas (*.csv *.txt *.xls *.xlsx);;CSV (*.csv);;Texto (*.txt);;Excel (*.xls *.xlsx)"
        )
        if ruta:
            self.le_file.setText(ruta)
            # auto-detectar y cargar columnas
            self._cargar_columnas()

    def _pick_out(self):
        carpeta = QFileDialog.getExistingDirectory(self, "Carpeta de salida")
        if carpeta:
            self.le_out.setText(carpeta)

    def _cargar_columnas(self):
        ruta = self.le_file.text().strip()
        if not ruta:
            QMessageBox.warning(self, "Aviso", "Selecciona primero un archivo.")
            return
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            _, columnas = _leer_tabla(
                ruta,
                hoja=self.le_hoja.text().strip() or 0,
                separador=self.le_sep.text() or ","
            )
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error leyendo tabla", str(e))
            return
        QApplication.restoreOverrideCursor()
        self._columnas = columnas

        for cb in (self.cb_grupo, self.cb_orden, self.cb_x, self.cb_y):
            cb.clear()
            cb.addItems(columnas)

        # heuristica: pre-seleccionar nombres comunes
        def _set_default(cb, candidatos):
            for cand in candidatos:
                for c in columnas:
                    if c.lower() == cand:
                        cb.setCurrentText(c)
                        return

        _set_default(self.cb_grupo, ["parcela", "agrupamiento", "grupo", "id"])
        _set_default(self.cb_orden, ["orden", "order", "secuencia", "vertice"])
        _set_default(self.cb_x, ["este_x", "este", "x", "longitud", "lon", "easting"])
        _set_default(self.cb_y, ["norte_y", "norte", "y", "latitud", "lat", "northing"])

    # --- collector ---
    def get_values(self):
        return {
            "archivo": self.le_file.text().strip(),
            "separador": self.le_sep.text() or ",",
            "hoja": self.le_hoja.text().strip() or "0",
            "campo_grupo": self.cb_grupo.currentText().strip(),
            "campo_orden": self.cb_orden.currentText().strip(),
            "campo_x": self.cb_x.currentText().strip(),
            "campo_y": self.cb_y.currentText().strip(),
            "crs": self.crs_widget.crs(),
            "carpeta": self.le_out.text().strip(),
            "cargar": self.chk_load.isChecked(),
        }


# ---------------------------------------------------------------
# QgsProcessingAlgorithm - permite usarse en la caja de procesos
# ---------------------------------------------------------------

class CrearPoligonosTabla(QgsProcessingAlgorithm):

    INPUT_FILE   = "INPUT_FILE"
    SHEET        = "SHEET"
    SEPARATOR    = "SEPARATOR"
    FIELD_GROUP  = "FIELD_GROUP"
    FIELD_ORDER  = "FIELD_ORDER"
    FIELD_X      = "FIELD_X"
    FIELD_Y      = "FIELD_Y"
    CRS          = "CRS"
    OUT_VERT     = "OUT_VERT"
    OUT_SEG      = "OUT_SEG"
    OUT_POL      = "OUT_POL"

    def name(self):
        return "crear_poligonos_tabla"

    def displayName(self):
        return "Crear poligonos a partir de tabla"

    def group(self):
        return "Geoprocesamiento"

    def groupId(self):
        return "geomaticape_geoprocesamiento"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "..",
                                  "Icons", "poligonos_tabla.png"))

    def createInstance(self):
        return CrearPoligonosTabla()

    def shortHelpString(self):
        return """
<h3>Crear poligonos a partir de tabla</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.8<br><br>

<b>Descripcion:</b><br>
Genera tres capas vectoriales a partir de una tabla CSV / TXT / XLS / XLSX:
<ul>
<li><b>Vertices</b> (puntos): Agrupamiento, Orden, Vertice (V1, V2...), Este_X, Norte_Y</li>
<li><b>Segmentos</b> (lineas): Agrupamiento, Desde, Hasta, Distancia_m, Azimut</li>
<li><b>Poligonos</b>: Agrupamiento, Area_ha, Perimetro_m</li>
</ul>

<b>Estructura esperada de la tabla:</b><br>
La tabla debe tener al menos 4 columnas: una de agrupamiento (parcela),
una de orden (numerico), una de coordenada X y una de coordenada Y.<br><br>

Si abres la herramienta desde el menu <i>Geomaticape -> Geoprocesamiento</i>
se mostrara un dialogo personalizado que carga las columnas reales de tu
tabla y te permite seleccionarlas en listas desplegables.<br>

<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FILE, "Tabla (CSV / TXT / XLS / XLSX)",
            extension="", fileFilter="Tablas (*.csv *.txt *.xls *.xlsx)"
        ))
        self.addParameter(QgsProcessingParameterString(
            self.SHEET, "Hoja Excel (nombre o indice 0..n)",
            defaultValue="0", optional=True
        ))
        self.addParameter(QgsProcessingParameterString(
            self.SEPARATOR, "Separador CSV/TXT",
            defaultValue=",", optional=True
        ))
        self.addParameter(QgsProcessingParameterString(
            self.FIELD_GROUP, "Campo de agrupamiento (parcela)",
            defaultValue="Parcela"
        ))
        self.addParameter(QgsProcessingParameterString(
            self.FIELD_ORDER, "Campo de orden (numerico)",
            defaultValue="Orden"
        ))
        self.addParameter(QgsProcessingParameterString(
            self.FIELD_X, "Campo coordenada X (Este)",
            defaultValue="Este_X"
        ))
        self.addParameter(QgsProcessingParameterString(
            self.FIELD_Y, "Campo coordenada Y (Norte)",
            defaultValue="Norte_Y"
        ))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, "CRS de salida",
            defaultValue="EPSG:32718"
        ))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.OUT_VERT, "Capa de vertices"
        ))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.OUT_SEG, "Capa de segmentos"
        ))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.OUT_POL, "Capa de poligonos"
        ))

    def processAlgorithm(self, parameters, context, feedback):
        ruta = self.parameterAsString(parameters, self.INPUT_FILE, context)
        hoja = self.parameterAsString(parameters, self.SHEET, context) or "0"
        sep = self.parameterAsString(parameters, self.SEPARATOR, context) or ","
        f_grp = self.parameterAsString(parameters, self.FIELD_GROUP, context)
        f_ord = self.parameterAsString(parameters, self.FIELD_ORDER, context)
        f_x = self.parameterAsString(parameters, self.FIELD_X, context)
        f_y = self.parameterAsString(parameters, self.FIELD_Y, context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        out_v = self.parameterAsOutputLayer(parameters, self.OUT_VERT, context)
        out_s = self.parameterAsOutputLayer(parameters, self.OUT_SEG, context)
        out_p = self.parameterAsOutputLayer(parameters, self.OUT_POL, context)

        feedback.pushInfo("====================================================")
        feedback.pushInfo("Crear poligonos a partir de tabla")
        feedback.pushInfo(f"Archivo : {ruta}")
        feedback.pushInfo(f"Campos  : grupo={f_grp}  orden={f_ord}  x={f_x}  y={f_y}")
        feedback.pushInfo(f"CRS     : {crs.authid()}")
        feedback.pushInfo("====================================================")

        feedback.setProgress(5)
        filas, columnas = _leer_tabla(ruta, hoja=hoja, separador=sep)
        feedback.pushInfo(f"Filas leidas: {len(filas)}")
        feedback.pushInfo(f"Columnas   : {columnas}")
        feedback.setProgress(25)

        lyr_v, lyr_s, lyr_p = _construir_capas(
            filas, f_grp, f_ord, f_x, f_y, crs
        )
        feedback.pushInfo(f"Vertices : {lyr_v.featureCount()}")
        feedback.pushInfo(f"Segmentos: {lyr_s.featureCount()}")
        feedback.pushInfo(f"Poligonos: {lyr_p.featureCount()}")
        feedback.setProgress(60)

        _guardar(lyr_v, out_v)
        _guardar(lyr_s, out_s)
        _guardar(lyr_p, out_p)
        feedback.setProgress(100)
        feedback.pushInfo("Proceso COMPLETADO.")

        return {
            self.OUT_VERT: out_v,
            self.OUT_SEG: out_s,
            self.OUT_POL: out_p,
        }

    # ---------- abre el dialogo personalizado desde el menu ----------
    def run(self):
        try:
            from qgis.utils import iface
            parent = iface.mainWindow() if iface else None
        except Exception:
            parent = None

        dlg = CrearPoligonosTablaDialog(parent)
        if dlg.exec_() != QDialog.Accepted:
            return

        v = dlg.get_values()
        if not v["archivo"]:
            QMessageBox.warning(parent, "Aviso", "Debes seleccionar una tabla.")
            return
        if not v["carpeta"]:
            QMessageBox.warning(parent, "Aviso", "Debes seleccionar la carpeta de salida.")
            return
        for k in ("campo_grupo", "campo_orden", "campo_x", "campo_y"):
            if not v[k]:
                QMessageBox.warning(parent, "Aviso", f"Selecciona el campo '{k}'.")
                return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            filas, _ = _leer_tabla(
                v["archivo"], hoja=v["hoja"], separador=v["separador"]
            )
            lyr_v, lyr_s, lyr_p = _construir_capas(
                filas, v["campo_grupo"], v["campo_orden"],
                v["campo_x"], v["campo_y"], v["crs"]
            )
            os.makedirs(v["carpeta"], exist_ok=True)
            base = os.path.splitext(os.path.basename(v["archivo"]))[0]
            ruta_v = os.path.join(v["carpeta"], f"{base}_vertices.shp")
            ruta_s = os.path.join(v["carpeta"], f"{base}_segmentos.shp")
            ruta_p = os.path.join(v["carpeta"], f"{base}_poligonos.shp")
            _guardar(lyr_v, ruta_v)
            _guardar(lyr_s, ruta_s)
            _guardar(lyr_p, ruta_p)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(parent, "Error", str(e))
            return
        QApplication.restoreOverrideCursor()

        if v["cargar"]:
            try:
                from qgis.utils import iface
                iface.addVectorLayer(ruta_v, f"{base}_vertices",  "ogr")
                iface.addVectorLayer(ruta_s, f"{base}_segmentos", "ogr")
                iface.addVectorLayer(ruta_p, f"{base}_poligonos", "ogr")
            except Exception:
                pass

        QMessageBox.information(
            parent, "Geomaticape",
            f"Proceso completado.\n\n"
            f"- {ruta_v}\n- {ruta_s}\n- {ruta_p}"
        )
