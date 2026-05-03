# -*- coding: utf-8 -*-
"""
mde_descargar_mde.py
Descarga MDE desde Google Earth Engine.

Estrategia dual automatica:
  - Area pequena: descarga directa getDownloadURL -> GeoTIFF local
  - Area grande : Export.toDrive -> carpeta GEE_Geomatica en Google Drive
                  con monitoreo en tiempo real del task

Limite real de GEE: 50331648 bytes (~48 MB) por request de descarga directa.
Equivale a ~25 MP para Int16 o ~12 MP para Float32.
Umbral conservador usado: 10 MP para garantizar compatibilidad con todos los tipos.

Geomaticape v1.24 - Geomatica Ambiental
"""
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProject,
    QgsRasterLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
)
import os
import time
import math

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────
# GEE rechaza getDownloadURL si el payload supera 50331648 bytes (~48 MB).
# SRTM/NASADEM = Int16 = 2 bytes/px  -> limite real ~25 MP
# Copernicus/ALOS = Float32 = 4 bytes/px -> limite real ~12 MP
# Usamos 10 MP como umbral conservador que funciona con todos los tipos de dato.
LIMITE_DIRECTO_MP  = 10_000_000   # pixeles — umbral para conmutar a Drive
GDRIVE_FOLDER      = 'GEE_Geomatica'
POLL_INTERVAL_S    = 12           # segundos entre consultas de estado

# ─────────────────────────────────────────────────────────────────────────────
# Catalogo de DEMs
# ─────────────────────────────────────────────────────────────────────────────
GEE_DEMS = [
    {
        'label'     : 'SRTM GL1 — 30 m global (NASA/USGS)',
        'collection': 'USGS/SRTMGL1_003',
        'band'      : 'elevation',
        'scale'     : 30,
        'tipo'      : 'Image',
        'dtype'     : 'Int16',
        'desc'      : 'SRTM 1 arc-sec. Global 56S-60N. Datum EGM96.',
    },
    {
        'label'     : 'Copernicus DEM GLO-30 — 30 m global',
        'collection': 'COPERNICUS/DEM/GLO30',
        'band'      : 'DEM',
        'scale'     : 30,
        'tipo'      : 'ImageCollection',
        'dtype'     : 'Float32',
        'desc'      : 'TanDEM-X. Cobertura global. Alta precision.',
    },
    {
        'label'     : 'NASADEM — 30 m global (NASA)',
        'collection': 'NASA/NASADEM_HGT/001',
        'band'      : 'elevation',
        'scale'     : 30,
        'tipo'      : 'Image',
        'dtype'     : 'Int16',
        'desc'      : 'SRTM reprocesado con ASTER/ICESat. Global.',
    },
    {
        'label'     : 'ALOS AW3D30 — 30 m global (JAXA)',
        'collection': 'JAXA/ALOS/AW3D30/V3_2',
        'band'      : 'DSM',
        'scale'     : 30,
        'tipo'      : 'ImageCollection',
        'dtype'     : 'Float32',
        'desc'      : 'DSM global de ALOS PRISM. JAXA.',
    },
    {
        'label'     : 'MERIT DEM — 90 m global (corregido)',
        'collection': 'MERIT/DEM/v1_0_3',
        'band'      : 'dem',
        'scale'     : 90,
        'tipo'      : 'Image',
        'dtype'     : 'Float32',
        'desc'      : 'SRTM/AW3D30 corregido por vegetacion y speckle.',
    },
    {
        'label'     : 'SRTM 90 m (CGIAR-CSI v4)',
        'collection': 'CGIAR/SRTM90_V4',
        'band'      : 'elevation',
        'scale'     : 90,
        'tipo'      : 'Image',
        'dtype'     : 'Int16',
        'desc'      : 'SRTM 90 m con vacios rellenados. CGIAR-CSI v4.',
    },
    {
        'label'     : 'HydroSHEDS — 90 m void-filled (WWF)',
        'collection': 'WWF/HydroSHEDS/03VFDEM',
        'band'      : 'b1',
        'scale'     : 90,
        'tipo'      : 'Image',
        'dtype'     : 'Int16',
        'desc'      : 'DEM hidrologicamente acondicionado. Tropicos/Subtropicos.',
    },
    {
        'label'     : 'ASTER GDEM v3 — 30 m global (NASA/METI)',
        'collection': 'NASA/ASTER_GED/AG100_003',
        'band'      : 'elevation',
        'scale'     : 30,
        'tipo'      : 'Image',
        'dtype'     : 'Float32',
        'desc'      : 'ASTER GDEM v3. Cobertura 83N-83S.',
    },
    {
        'label'     : '3DEP 1 m (solo EE.UU.) — USGS',
        'collection': 'USGS/3DEP/1m',
        'band'      : 'elevation',
        'scale'     : 1,
        'tipo'      : 'ImageCollection',
        'dtype'     : 'Float32',
        'desc'      : '3D Elevation Program 1 metro. Solo EE.UU.',
    },
]

DEM_LABELS = [d['label'] for d in GEE_DEMS]

# Bytes por pixel segun dtype
DTYPE_BYTES = {'Int16': 2, 'Int32': 4, 'Float32': 4, 'Float64': 8, 'Byte': 1}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_ee(feedback):
    try:
        import ee
        return ee
    except ImportError:
        raise QgsProcessingException(
            "Falta el modulo 'earthengine-api'.\n"
            "Instalalo en la OSGeo4W Shell (Windows):\n"
            "   python -m pip install earthengine-api\n"
            "Linux / macOS:\n"
            "   pip install earthengine-api"
        )


def _autenticar_gee(ee, correo, proyecto, feedback):
    try:
        kwargs = {}
        if proyecto.strip():
            kwargs['project'] = proyecto.strip()

        if correo.strip():
            feedback.pushInfo(f"Conectando GEE | cuenta: {correo}")
            feedback.pushInfo(f"Proyecto       : {proyecto or '(por defecto)'}")
            ee.Initialize(opt_url='https://earthengine.googleapis.com', **kwargs)
        else:
            feedback.pushInfo("Conectando GEE con credenciales locales guardadas...")
            ee.Initialize(**kwargs)

        feedback.pushInfo("Conexion a Google Earth Engine exitosa.")

    except Exception as ex:
        msg = str(ex)
        if any(k in msg.lower() for k in ('authorize', 'credentials', 'oauth', 'token')):
            raise QgsProcessingException(
                "No se encontraron credenciales GEE validas.\n\n"
                "Ejecuta UNA VEZ en la Consola Python de QGIS:\n"
                "   import ee\n"
                "   ee.Authenticate()                          # abre navegador\n"
                "   ee.Initialize(project='TU_PROYECTO_GEE')  # valida la sesion\n\n"
                "Luego vuelve a ejecutar esta herramienta."
            )
        raise QgsProcessingException(f"Error al conectar GEE: {msg}")


def _construir_imagen(ee, dem_info, region):
    cid  = dem_info['collection']
    band = dem_info['band']
    if dem_info['tipo'] == 'Image':
        img = ee.Image(cid).select(band)
    else:
        img = (ee.ImageCollection(cid)
                 .filterBounds(region)
                 .mosaic()
                 .select(band))
    return img.clip(region)


def _estimar_pixeles(lon_min, lat_min, lon_max, lat_max, scale):
    """Pixeles con correccion coseno latitudinal."""
    lat_med = (lat_min + lat_max) / 2.0
    cos_lat = math.cos(math.radians(lat_med))
    ancho_m = (lon_max - lon_min) * 111320.0 * cos_lat
    alto_m  = (lat_max - lat_min) * 111320.0
    px_x    = max(1, int(ancho_m / scale))
    px_y    = max(1, int(alto_m  / scale))
    return px_x * px_y, px_x, px_y


def _estimar_bytes(total_px, dtype):
    """Peso estimado del raster en bytes."""
    bpp = DTYPE_BYTES.get(dtype, 4)
    return total_px * bpp


def _descarga_directa(ee, imagen, scale, region, output_path, feedback):
    """getDownloadURL -> GeoTIFF local. Maneja ZIP o TIF directo."""
    import zipfile, os, shutil
    from urllib.parse import urlparse
    import requests

    feedback.pushInfo("Generando URL de descarga directa en GEE...")
    try:
        url = imagen.getDownloadURL({
            'scale': scale,
            'region': region,
            'format': 'GEO_TIFF',
            'crs': 'EPSG:4326',
        })
    except Exception as ex:
        msg = str(ex)
        if '50331648' in msg or 'request size' in msg.lower() or 'must be less than' in msg.lower():
            raise QgsProcessingException(
                "GEE rechaza la descarga directa porque el archivo supera 48 MB.\n"
                "Reduce el área o aumenta la escala.\n\n"
                f"Detalle GEE: {msg}"
            )
        raise QgsProcessingException(f"Error al generar URL: {msg}")

    # 🔒 FIX SEGURIDAD (Bandit B310)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise QgsProcessingException(
            f"Esquema de URL no permitido: {parsed.scheme}"
        )

    feedback.pushInfo("Descargando GeoTIFF desde GEE (modo seguro)...")
    tmp = output_path + '_tmp.bin'

    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            descargado = 0

            with open(tmp, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        descargado += len(chunk)

                        if total > 0:
                            progreso = int(descargado * 100 / total)
                            feedback.setProgress(min(progreso, 100))

    except Exception as ex:
        raise QgsProcessingException(f"Error en descarga: {str(ex)}")

    # ── Procesar archivo descargado ─────────────────────────────
    if zipfile.is_zipfile(tmp):
        feedback.pushInfo("Descomprimiendo ZIP de GEE...")
        with zipfile.ZipFile(tmp, 'r') as zf:
            tifs = [n for n in zf.namelist() if n.lower().endswith('.tif')]
            if not tifs:
                raise QgsProcessingException("El ZIP no contiene archivos .tif")

            extracted = zf.extract(tifs[0], os.path.dirname(output_path) or '.')
        shutil.move(extracted, output_path)
        os.remove(tmp)
    else:
        shutil.move(tmp, output_path)

    feedback.pushInfo(f"MDE guardado en: {output_path}")


def _exportar_drive(ee, imagen, scale, region, task_name, feedback):
    """
    Export.image.toDrive + monitoreo en tiempo real.
    La carpeta GEE_Geomatica se crea automaticamente si no existe.
    """
    feedback.pushInfo("=" * 58)
    feedback.pushInfo("  AREA GRANDE: exportando a Google Drive")
    feedback.pushInfo(f"  Carpeta : {GDRIVE_FOLDER}")
    feedback.pushInfo(f"  Archivo : {task_name}.tif")
    feedback.pushInfo("=" * 58)

    task = ee.batch.Export.image.toDrive(
        image          = imagen,
        description    = task_name,
        folder         = GDRIVE_FOLDER,
        fileNamePrefix = task_name,
        scale          = scale,
        region         = region,
        crs            = 'EPSG:4326',
        fileFormat     = 'GeoTIFF',
        maxPixels      = 1e13,
    )
    task.start()

    feedback.pushInfo(f"Task GEE iniciado. ID: {task.id}")
    feedback.pushInfo(
        "Monitorea el progreso en:\n"
        "  https://code.earthengine.google.com/tasks"
    )
    feedback.pushInfo("Esperando que GEE complete la exportacion...")

    ESTADOS_FIN = {'COMPLETED', 'FAILED', 'CANCELLED', 'CANCEL_REQUESTED'}
    ultimo_estado = ''
    espera_total  = 0
    spinner       = ['|', '/', '-', '\\']
    spin_idx      = 0

    while True:
        time.sleep(POLL_INTERVAL_S)
        espera_total += POLL_INTERVAL_S

        if feedback.isCanceled():
            feedback.pushInfo("Cancelando task en GEE...")
            task.cancel()
            raise QgsProcessingException("Proceso cancelado por el usuario.")

        try:
            status   = task.status()
            estado   = status.get('state', 'UNKNOWN')
            progreso = status.get('progress', 0.0)
        except Exception as ex:
            feedback.pushWarning(f"No se pudo consultar el estado del task: {ex}")
            continue

        if estado != ultimo_estado:
            feedback.pushInfo(f"  Estado GEE : {estado}")
            ultimo_estado = estado

        mins = espera_total // 60
        segs = espera_total % 60
        spin = spinner[spin_idx % len(spinner)]
        spin_idx += 1

        if progreso and progreso > 0:
            feedback.setProgress(int(progreso * 100))
            feedback.pushInfo(
                f"  {spin} Progreso: {progreso*100:.1f}%  |  "
                f"Tiempo transcurrido: {mins}m {segs}s"
            )
        else:
            feedback.pushInfo(
                f"  {spin} Procesando en GEE...  |  "
                f"Tiempo transcurrido: {mins}m {segs}s"
            )

        if estado in ESTADOS_FIN:
            break

    if estado == 'COMPLETED':
        feedback.pushInfo("=" * 58)
        feedback.pushInfo("  Exportacion COMPLETADA exitosamente.")
        feedback.pushInfo(f"  Archivo: {task_name}.tif")
        feedback.pushInfo(f"  Carpeta Google Drive: {GDRIVE_FOLDER}")
        feedback.pushInfo("")
        feedback.pushInfo("  Para usarlo en QGIS:")
        feedback.pushInfo("  1. Abre https://drive.google.com")
        feedback.pushInfo(f"  2. Ve a la carpeta '{GDRIVE_FOLDER}'")
        feedback.pushInfo(f"  3. Descarga '{task_name}.tif'")
        feedback.pushInfo("  4. Arrastralo al proyecto QGIS o usa")
        feedback.pushInfo("     Capa > Anadir capa raster.")
        feedback.pushInfo("=" * 58)
        return True

    error_msg = task.status().get('error_message', 'Error desconocido')
    raise QgsProcessingException(
        f"La exportacion a Google Drive fallo.\n"
        f"Estado: {estado}\n"
        f"Detalle: {error_msg}\n\n"
        f"Revisa el panel de tasks:\n"
        f"  https://code.earthengine.google.com/tasks"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Algoritmo QGIS Processing
# ─────────────────────────────────────────────────────────────────────────────

class MDEDescargarMDE(QgsProcessingAlgorithm):

    EXTENT  = 'EXTENT'
    EMAIL   = 'EMAIL'
    PROJECT = 'PROJECT'
    DATASET = 'DATASET'
    SCALE   = 'SCALE'
    OPEN    = 'OPEN'
    OUTPUT  = 'OUTPUT'

    def createInstance(self):
        return MDEDescargarMDE()

    def name(self):
        return 'mde_descargar_mde'

    def displayName(self):
        return 'Descargar MDE'

    def group(self):
        return 'MDE'

    def groupId(self):
        return 'mde_geo'

    def tags(self):
        return ['mde', 'dem', 'elevacion', 'srtm', 'copernicus', 'nasadem',
                'alos', 'merit', 'aster', '3dep', 'gee', 'google earth engine',
                'google drive', 'descarga', 'modelo', 'terreno', 'digital']

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  'Icons', 'extraer_valores.png'))

    def shortHelpString(self):
        catalogo = ''.join(
            f'<li><b>{d["label"]}</b><br>'
            f'<small>{d["desc"]} | Nativo: {d["scale"]} m | Tipo: {d["dtype"]}</small></li>'
            for d in GEE_DEMS
        )
        limite_mp = LIMITE_DIRECTO_MP // 1_000_000
        return f"""
<b>Descargar MDE desde Google Earth Engine</b><br><br>

La herramienta selecciona automaticamente el metodo de descarga:<br><br>

<table border="1" cellpadding="4" style="border-collapse:collapse;">
<tr style="background:#ddd"><th>Condicion</th><th>Metodo</th></tr>
<tr><td>Area &le; {limite_mp} MP</td>
    <td><b>Descarga directa</b> → GeoTIFF al disco local</td></tr>
<tr><td>Area &gt; {limite_mp} MP</td>
    <td><b>Google Drive</b> → carpeta <code>{GDRIVE_FOLDER}</code><br>
        <small>Monitoreo en tiempo real. Luego descarga desde drive.google.com</small></td></tr>
</table><br>

<b>Limite tecnico de GEE:</b> getDownloadURL acepta hasta 48 MB por request
(~25 MP en Int16, ~12 MP en Float32). El umbral de {limite_mp} MP garantiza
compatibilidad con todos los tipos de dato.<br><br>

<b>Configuracion inicial (una sola vez):</b><br>
<ol>
  <li>Cuenta aprobada en
      <a href="https://earthengine.google.com">earthengine.google.com</a></li>
  <li><code>python -m pip install earthengine-api</code></li>
  <li>En la consola Python de QGIS:<br>
      <code>import ee</code><br>
      <code>ee.Authenticate()   # abre navegador y guarda token</code><br>
      <code>ee.Initialize(project='mi-proyecto-gee')</code>
  </li>
</ol>

<b>DEMs disponibles:</b><br><ul>{catalogo}</ul>
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString(
            self.EMAIL,
            'Correo de cuenta Google Earth Engine',
            defaultValue='',
            optional=True
        ))
        self.addParameter(QgsProcessingParameterString(
            self.PROJECT,
            'ID del proyecto GEE (Google Cloud Project)',
            defaultValue='',
            optional=True
        ))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            'Extension del area de interes'
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.DATASET,
            'Fuente de elevacion (DEM)',
            options=DEM_LABELS,
            defaultValue=0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SCALE,
            'Escala de salida (m)  —  0 = resolucion nativa del DEM',
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=0,
            minValue=0,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.OPEN,
            'Cargar MDE en QGIS al finalizar (solo descarga directa)',
            defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT,
            'Archivo de salida local (solo descarga directa)',
            fileFilter='GeoTIFF (*.tif)'
        ))

    def processAlgorithm(self, parameters, context, feedback):
        correo   = self.parameterAsString(parameters, self.EMAIL,   context).strip()
        proyecto = self.parameterAsString(parameters, self.PROJECT, context).strip()
        idx_dem  = self.parameterAsEnum(parameters,  self.DATASET,  context)
        escala   = self.parameterAsInt(parameters,   self.SCALE,    context)
        output   = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        carregar = self.parameterAsBool(parameters,  self.OPEN,     context)

        ext     = self.parameterAsExtent(parameters, self.EXTENT, context)
        crs_ext = self.parameterAsExtentCrs(parameters, self.EXTENT, context)

        # ── Reproyectar a WGS84 ───────────────────────────────────
        crs_geo = QgsCoordinateReferenceSystem('EPSG:4326')
        if not crs_ext.isGeographic():
            tr = QgsCoordinateTransform(crs_ext, crs_geo, QgsProject.instance())
            pmin = tr.transform(ext.xMinimum(), ext.yMinimum())
            pmax = tr.transform(ext.xMaximum(), ext.yMaximum())
            lon_min, lat_min = pmin.x(), pmin.y()
            lon_max, lat_max = pmax.x(), pmax.y()
        else:
            lon_min, lat_min = ext.xMinimum(), ext.yMinimum()
            lon_max, lat_max = ext.xMaximum(), ext.yMaximum()

        dem_info  = GEE_DEMS[idx_dem]
        scale_eff = escala if escala > 0 else dem_info['scale']

        # ── Estimar tamano ─────────────────────────────────────────
        total_px, px_x, px_y = _estimar_pixeles(
            lon_min, lat_min, lon_max, lat_max, scale_eff)
        peso_mb = _estimar_bytes(total_px, dem_info['dtype']) / 1_048_576

        feedback.pushInfo('─' * 58)
        feedback.pushInfo(f"DEM             : {dem_info['label']}")
        feedback.pushInfo(f"Coleccion GEE   : {dem_info['collection']}")
        feedback.pushInfo(f"Banda / Tipo    : {dem_info['band']} / {dem_info['dtype']}")
        feedback.pushInfo(f"Escala efectiva : {scale_eff} m")
        feedback.pushInfo(f"Bbox WGS84      : [{lon_min:.5f}, {lat_min:.5f}, "
                          f"{lon_max:.5f}, {lat_max:.5f}]")
        feedback.pushInfo(f"Dimension aprox : {px_y} x {px_x} px  "
                          f"({total_px/1e6:.2f} MP  |  ~{peso_mb:.1f} MB)")
        feedback.pushInfo('─' * 58)

        # ── Decision: directo o Drive ─────────────────────────────
        usar_drive = total_px > LIMITE_DIRECTO_MP

        if usar_drive:
            feedback.pushInfo(
                f"AVISO: El area ({total_px/1e6:.1f} MP / ~{peso_mb:.0f} MB) "
                f"supera el limite de descarga directa de GEE (~48 MB).\n"
                f"Se enviara automaticamente a Google Drive\n"
                f"en la carpeta '{GDRIVE_FOLDER}'."
            )
        else:
            feedback.pushInfo(
                f"Tamano dentro del limite ({peso_mb:.1f} MB < 48 MB). "
                "Descarga directa."
            )

        # ── Autenticar GEE ────────────────────────────────────────
        ee = _ensure_ee(feedback)
        _autenticar_gee(ee, correo, proyecto, feedback)

        # ── Construir imagen ──────────────────────────────────────
        region = ee.Geometry.Rectangle([lon_min, lat_min, lon_max, lat_max])
        imagen = _construir_imagen(ee, dem_info, region)

        # ── Ejecutar ──────────────────────────────────────────────
        if usar_drive:
            import datetime
            ts        = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            dem_slug  = dem_info['collection'].replace('/', '_')
            task_name = f"MDE_{dem_slug}_{ts}"

            _exportar_drive(ee, imagen, scale_eff, region, task_name, feedback)

            self._output    = None
            self._carregar  = False
            self._via_drive = True
            self._task_name = task_name
            return {}

        else:
            _descarga_directa(ee, imagen, scale_eff, region, output, feedback)
            self._output    = output
            self._carregar  = carregar
            self._via_drive = False
            self._dem_name  = dem_info['label']
            return {self.OUTPUT: output}

    def postProcessAlgorithm(self, context, feedback):
        if getattr(self, '_via_drive', False):
            return {}

        if getattr(self, '_carregar', False) and getattr(self, '_output', None):
            nombre = self._dem_name.split('—')[0].strip()
            rlayer = QgsRasterLayer(self._output, f'MDE - {nombre}')
            if rlayer.isValid():
                QgsProject.instance().addMapLayer(rlayer)
            else:
                feedback.pushWarning(
                    'El raster se descargo pero no pudo cargarse automaticamente.\n'
                    f'Cargalo manualmente desde: {self._output}'
                )
        return {}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
