# -*- coding: utf-8 -*-
"""
raster_mosaico_imagenes.py  ·  GeomaticaPE v1.2.5
==================================================
Mosaico de imágenes multiespectrales con flujo propio de Geomatica:

  1. [Opcional] Recorte individual por zona de estudio (shapefile/GeoJSON)
     → reproyección automática del vector al CRS de cada imagen
  2. Limpieza de NoData → float32 / NaN (sin alterar valores válidos)
  3. Mosaico con método e interpolación configurables

Requiere: rasterio, geopandas (incluidos en OSGeo4W estándar).
"""

import os
import shutil
import tempfile
import numpy as np

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFileDestination,
    QgsProcessing,
    QgsProject,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsCoordinateTransformContext,
)


# ──────────────────────────────────────────────────────────────────────────────
_METODOS = ['first', 'last', 'min', 'max', 'sum']
_METODOS_LABEL = [
    'first  – primer píxel válido (recomendado para solapamientos sin nubes)',
    'last   – último píxel válido',
    'min    – valor mínimo en zona de solapamiento',
    'max    – valor máximo en zona de solapamiento',
    'sum    – suma en zona de solapamiento',
]

_RESAMPLE_KEYS = ['nearest', 'bilinear', 'cubic', 'lanczos', 'average', 'mode']
_RESAMPLE_LABEL = [
    'Vecino más cercano  (categórico / preserva valores exactos)',
    'Bilineal            (continuo, suavizado 2×2)',
    'Cúbico              (suavizado mayor, interpolación 4×4)',
    'Lanczos             (alta calidad, mayor costo)',
    'Promedio            (reducción de resolución)',
    'Moda                (clasificaciones / datos discretos)',
]


# ──────────────────────────────────────────────────────────────────────────────

class RasterMosaicoImagenes(QgsProcessingAlgorithm):

    RASTERLIST = 'RASTERLIST'
    RESOLUTION = 'RESOLUTION'
    METODO     = 'METODO'
    RESAMPLING = 'RESAMPLING'
    FRAME      = 'FRAME'
    MOSAIC     = 'MOSAIC'
    OPEN       = 'OPEN'

    def createInstance(self):
        return RasterMosaicoImagenes()

    def name(self):
        return 'raster_mosaico_imagenes'

    def displayName(self):
        return 'Mosaico de imágenes'

    def group(self):
        return 'Ráster'

    def groupId(self):
        return 'raster_geo'

    def tags(self):
        return ['mosaico', 'merge', 'combinar', 'imagen', 'raster',
                'multiespectral', 'recorte', 'zona', 'sentinel', 'landsat',
                'clip', 'nodata', 'rasterio']

    def icon(self):
        return QIcon(os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'Icons', 'indices.png'
        ))

    def shortHelpString(self):
        return (
            '<b>Mosaico de imágenes multiespectrales</b><br>'
            'Flujo propio GeomaticaPE (rasterio):<br><br>'
            '<ol>'
            '<li><b>[Opcional]</b> Recorta cada imagen a la zona de estudio '
            '(shapefile/GeoJSON). Reproyección automática si el CRS difiere.</li>'
            '<li>Convierte a <code>float32</code> y reemplaza NoData por NaN '
            'sin modificar píxeles válidos.</li>'
            '<li>Genera el mosaico con el método e interpolación elegidos. '
            'Conserva los nombres de bandas del original.</li>'
            '</ol>'
            '<b>Resolución:</b> 0 = mantiene la resolución original de las imágenes.<br>'
            '<b>Zona de estudio:</b> si se define, el recorte se aplica antes del mosaico.<br><br>'
            '<i>Dependencias: rasterio · geopandas (incluidas en OSGeo4W).</i>'
        )

    # ── Parámetros ─────────────────────────────────────────────────────────────

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.RASTERLIST,
                'Imágenes multiespectrales a mosaiquear',
                layerType=QgsProcessing.TypeRaster
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.RESOLUTION,
                'Resolución de salida (m)  —  0 = resolución original',
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.METODO,
                'Método de mosaico (tratamiento de solapamiento)',
                options=_METODOS_LABEL,
                defaultValue=0
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESAMPLING,
                'Método de interpolación (remuestreo)',
                options=_RESAMPLE_LABEL,
                defaultValue=0
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.FRAME,
                'Zona de estudio – recorte previo al mosaico (opcional)',
                types=[QgsProcessing.TypeVectorPolygon],
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.MOSAIC,
                'Mosaico de salida',
                fileFilter='GeoTIFF (*.tif *.tiff)'
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.OPEN,
                'Cargar mosaico en el proyecto QGIS al finalizar',
                defaultValue=True
            )
        )

    # ── Procesamiento ──────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):

        # Importaciones diferidas para no bloquear carga del plugin
        try:
            import rasterio
            from rasterio.mask       import mask as rio_mask
            from rasterio.merge      import merge as rio_merge
            from rasterio.enums      import Resampling
            from rasterio.warp       import reproject as rio_reproject
            from rasterio.transform  import from_bounds, array_bounds
        except ImportError:
            raise QgsProcessingException(
                'rasterio no encontrado.\n'
                'Instalar desde OSGeo4W Shell:\n'
                '  pip install rasterio --break-system-packages'
            )

        try:
            import geopandas as gpd
            HAS_GPD = True
        except ImportError:
            HAS_GPD = False

        # Leer parámetros
        layers     = self.parameterAsLayerList(parameters, self.RASTERLIST, context)
        resolucion = self.parameterAsDouble(parameters, self.RESOLUTION, context)
        met_idx    = self.parameterAsEnum(parameters, self.METODO, context)
        res_idx    = self.parameterAsEnum(parameters, self.RESAMPLING, context)
        vlayer     = self.parameterAsVectorLayer(parameters, self.FRAME, context)
        output     = self.parameterAsFileOutput(parameters, self.MOSAIC, context)
        cargar     = self.parameterAsBool(parameters, self.OPEN, context)

        metodo        = _METODOS[met_idx]
        resamp_key    = _RESAMPLE_KEYS[res_idx]
        resample_enum = {
            'nearest':  Resampling.nearest,
            'bilinear': Resampling.bilinear,
            'cubic':    Resampling.cubic,
            'lanczos':  Resampling.lanczos,
            'average':  Resampling.average,
            'mode':     Resampling.mode,
        }[resamp_key]

        if not layers:
            raise QgsProcessingException('Selecciona al menos una imagen.')

        # Rutas de los layers QGIS → rutas en disco
        imagenes = [lyr.dataProvider().dataSourceUri().split('|')[0] for lyr in layers]
        feedback.pushInfo(f'📂 {len(imagenes)} imagen(es) cargada(s)')

        tmp_dir = tempfile.mkdtemp(prefix='geomatica_mosaico_')
        feedback.pushInfo(f'📁 Temp: {tmp_dir}')

        lib = dict(
            rasterio=rasterio, rio_mask=rio_mask, rio_merge=rio_merge,
            resample_enum=resample_enum, rio_reproject=rio_reproject,
            from_bounds=from_bounds, array_bounds=array_bounds
        )

        try:
            imagenes_trabajo = imagenes

            # PASO 1 – Recorte opcional
            if vlayer:
                if not HAS_GPD:
                    raise QgsProcessingException(
                        'geopandas es necesario para el recorte por zona.\n'
                        'Instalar: pip install geopandas --break-system-packages'
                    )
                zona_tmp = os.path.join(tmp_dir, 'zona.gpkg')
                self._exportar_vlayer(vlayer, zona_tmp, context)
                gdf = gpd.read_file(zona_tmp)
                feedback.pushInfo(f'✂️  Zona: {vlayer.name()}')
                imagenes_trabajo = self._recortar(
                    imagenes, gdf, tmp_dir, resolucion, lib, feedback
                )
            elif resolucion > 0:
                feedback.pushInfo('🔄 Solo remuestreo (sin zona de recorte)')
                imagenes_trabajo = self._remuestrear(
                    imagenes, tmp_dir, resolucion, lib, feedback
                )
            else:
                feedback.pushInfo('⏭  Sin recorte ni cambio de resolución')

            if feedback.isCanceled():
                return {}

            # PASO 2 – Limpieza NoData
            feedback.pushInfo('🧼 Limpiando NoData...')
            imagenes_clean = self._limpiar_nodata(
                imagenes_trabajo, tmp_dir, rasterio, feedback
            )

            if feedback.isCanceled():
                return {}

            # PASO 3 – Mosaico
            feedback.pushInfo(f'🧩 Mosaico — método: {metodo} | interpolación: {resamp_key}')
            self._mosaico(
                imagenes_clean, metodo, resample_enum, output,
                rasterio, rio_merge, feedback
            )

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            feedback.pushInfo('🗑  Temporales eliminados')

        feedback.pushInfo(f'🎉 Listo: {output}')
        feedback.setProgress(100)

        self._output = output
        self._cargar = cargar
        return {self.MOSAIC: output}

    def postProcessAlgorithm(self, context, feedback):
        if self._cargar and os.path.isfile(self._output):
            rl = QgsRasterLayer(self._output, 'Mosaico de imágenes')
            if rl.isValid():
                QgsProject.instance().addMapLayer(rl)
        return {}

    # ── Recorte ────────────────────────────────────────────────────────────────

    def _recortar(self, imagenes, gdf, tmp_dir, resolucion, lib, feedback):
        rasterio     = lib['rasterio']
        rio_mask     = lib['rio_mask']
        resample_enum = lib['resample_enum']
        n = len(imagenes)

        with rasterio.open(imagenes[0]) as ref:
            band_names = ref.descriptions or []

        clipped = []
        for i, img_path in enumerate(imagenes):
            if feedback.isCanceled():
                return clipped

            feedback.setProgress(int(5 + (i / n) * 28))
            nombre = os.path.basename(img_path)
            feedback.pushInfo(f'  ✂️  {i+1}/{n}: {nombre}')

            with rasterio.open(img_path) as src:
                # Reproyectar GDF si CRS difiere
                try:
                    src_epsg = src.crs.to_epsg()
                    gdf_use = gdf.to_crs(epsg=src_epsg) if src_epsg else gdf.to_crs(src.crs.to_wkt())
                except Exception:
                    gdf_use = gdf

                geometries = gdf_use.geometry.values
                out_image, out_transform = rio_mask(src, geometries, crop=True)
                out_meta = src.meta.copy()
                out_meta.update({
                    'height':    out_image.shape[1],
                    'width':     out_image.shape[2],
                    'transform': out_transform,
                })

                if resolucion > 0:
                    out_image, out_transform, out_meta = self._aplicar_resolucion(
                        out_image, out_transform, out_meta, resolucion, lib
                    )

            out_path = os.path.join(
                tmp_dir,
                nombre.replace('.tiff', '.tif').replace('.tif', '_clip.tif')
            )
            with rasterio.open(out_path, 'w', **out_meta) as dst:
                dst.write(out_image)
                for idx, bn in enumerate(band_names):
                    if bn:
                        dst.set_band_description(idx + 1, bn)

            clipped.append(out_path)

        feedback.pushInfo('  ✅ Recorte completado')
        return clipped

    # ── Solo remuestreo ────────────────────────────────────────────────────────

    def _remuestrear(self, imagenes, tmp_dir, resolucion, lib, feedback):
        rasterio = lib['rasterio']
        n = len(imagenes)

        with rasterio.open(imagenes[0]) as ref:
            band_names = ref.descriptions or []

        resultado = []
        for i, img_path in enumerate(imagenes):
            if feedback.isCanceled():
                return resultado

            feedback.setProgress(int(5 + (i / n) * 18))
            nombre = os.path.basename(img_path)
            feedback.pushInfo(f'  🔄 {i+1}/{n}: {nombre}')

            with rasterio.open(img_path) as src:
                data = src.read()
                meta = src.meta.copy()
                transform = src.transform
                data, transform, meta = self._aplicar_resolucion(
                    data, transform, meta, resolucion, lib
                )

            out_path = os.path.join(
                tmp_dir,
                nombre.replace('.tiff', '.tif').replace('.tif', '_res.tif')
            )
            with rasterio.open(out_path, 'w', **meta) as dst:
                dst.write(data)
                for idx, bn in enumerate(band_names):
                    if bn:
                        dst.set_band_description(idx + 1, bn)

            resultado.append(out_path)

        return resultado

    # ── Aplicar resolución custom ──────────────────────────────────────────────

    def _aplicar_resolucion(self, data, transform, meta, resolucion, lib):
        from math import ceil
        array_bounds  = lib['array_bounds']
        from_bounds   = lib['from_bounds']
        rio_reproject = lib['rio_reproject']
        resample_enum = lib['resample_enum']

        bounds = array_bounds(meta['height'], meta['width'], transform)
        minx, miny, maxx, maxy = bounds

        nw = max(1, ceil((maxx - minx) / resolucion))
        nh = max(1, ceil((maxy - miny) / resolucion))
        new_transform = from_bounds(minx, miny, maxx, maxy, nw, nh)

        n_bands  = data.shape[0]
        new_data = np.empty((n_bands, nh, nw), dtype=data.dtype)

        rio_reproject(
            source=data,
            destination=new_data,
            src_transform=transform,
            src_crs=meta['crs'],
            dst_transform=new_transform,
            dst_crs=meta['crs'],
            resampling=resample_enum,
        )

        meta.update(height=nh, width=nw, transform=new_transform)
        return new_data, new_transform, meta

    # ── Limpieza NoData ────────────────────────────────────────────────────────

    def _limpiar_nodata(self, imagenes, tmp_dir, rasterio, feedback):
        clean = []
        n = len(imagenes)

        for i, fp in enumerate(imagenes):
            if feedback.isCanceled():
                return clean

            feedback.setProgress(int(35 + (i / n) * 20))

            with rasterio.open(fp) as src:
                data       = src.read().astype('float32')
                nodata     = src.nodata
                meta       = src.meta.copy()
                band_names = src.descriptions or []

            if nodata is not None:
                data[data == nodata] = np.nan

            meta.update(dtype='float32', nodata=np.nan)

            clean_path = fp.replace('.tif', '_clean.tif')
            with rasterio.open(clean_path, 'w', **meta) as dst:
                dst.write(data)
                for idx, bn in enumerate(band_names):
                    if bn:
                        dst.set_band_description(idx + 1, bn)

            clean.append(clean_path)

        feedback.pushInfo('  ✅ NoData limpio')
        return clean

    # ── Mosaico ────────────────────────────────────────────────────────────────

    def _mosaico(self, imagenes, metodo, resample_enum, salida,
                 rasterio, rio_merge, feedback):

        src_files = []
        try:
            feedback.setProgress(58)
            src_files = [rasterio.open(fp) for fp in imagenes]
            band_names = src_files[0].descriptions or []

            feedback.setProgress(62)
            mosaic, out_trans = rio_merge(
                src_files,
                method=metodo,
                resampling=resample_enum,
                nodata=src_files[0].nodata,
            )

            feedback.setProgress(88)
            out_meta = src_files[0].meta.copy()
            out_meta.update({
                'height':    mosaic.shape[1],
                'width':     mosaic.shape[2],
                'transform': out_trans,
                'dtype':     'float32',
                'nodata':    np.nan,
                'compress':  'lzw',
            })

            os.makedirs(os.path.dirname(os.path.abspath(salida)), exist_ok=True)

            with rasterio.open(salida, 'w', **out_meta) as dest:
                dest.write(mosaic)
                for idx, bn in enumerate(band_names):
                    if bn:
                        dest.set_band_description(idx + 1, bn)

        finally:
            for s in src_files:
                try:
                    s.close()
                except Exception:
                    pass

    # ── Exportar vlayer → gpkg ─────────────────────────────────────────────────

    def _exportar_vlayer(self, vlayer, out_path, context):
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName  = 'GPKG'
        opts.fileEncoding = 'UTF-8'
        QgsVectorFileWriter.writeAsVectorFormatV3(
            vlayer, out_path, QgsCoordinateTransformContext(), opts
        )

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
