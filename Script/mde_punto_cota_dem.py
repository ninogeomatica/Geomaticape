# -*- coding: utf-8 -*-
"""
mde_punto_cota_dem.py
Genera elevaciones puntuales (picos y depresiones) a partir de un MDE
y una capa de curvas de nivel cerradas.
Adaptado de LFTools - Relief_SpotElevation (Leandro Franca) para Geomaticape.
"""
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsFeatureSink,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsProject,
)
from osgeo import gdal
import numpy as np
import os


class MDEPuntoCotaDEM(QgsProcessingAlgorithm):

    INPUT    = 'INPUT'
    CONTOURS = 'CONTOURS'
    FIELD    = 'FIELD'
    SPOTS    = 'SPOTS'

    def createInstance(self):
        return MDEPuntoCotaDEM()

    def name(self):
        return 'mde_punto_cota_dem'

    def displayName(self):
        return 'Generar elevaciones puntuales'

    def group(self):
        return 'MDE'

    def groupId(self):
        return 'mde_geo'

    def tags(self):
        return ['cota', 'elevacion', 'pico', 'depresion', 'spot elevation',
                'mde', 'dem', 'curvas de nivel', 'altimetria', 'topo']

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  'Icons', 'extraer_valores.png'))

    def shortHelpString(self):
        return (
            '<b>Generar elevaciones puntuales</b><br><br>'
            'Genera una capa de <b>puntos cotados</b> (picos y depresiones) a partir '
            'de un <b>MDE raster</b> y una capa de <b>curvas de nivel vectoriales</b>.<br><br>'
            '<b>Logica:</b><br>'
            'La herramienta detecta curvas cerradas (anillos), determina si el interior '
            'de cada anillo tiene valores mas altos o mas bajos que la cota de la curva, '
            'y coloca un punto en el pixel maximo (pico) o minimo (depresion) dentro del anillo.<br><br>'
            '<b>Atributos de salida:</b><br>'
            '- <b>elevacion:</b> valor del pixel del MDE (metros).<br>'
            '- <b>tipo:</b> 1 = pico, -1 = depresion.<br><br>'
            '<b>Requisito:</b> matplotlib (incluido en QGIS 3.22+).<br><br>'
            'Adaptado de LFTools - Leandro Franca.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT,
            'MDE raster de entrada',
            [QgsProcessing.TypeRaster]
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.CONTOURS,
            'Curvas de nivel (capa de lineas)',
            [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD,
            'Campo con el valor de cota',
            parentLayerParameterName=self.CONTOURS,
            type=QgsProcessingParameterField.Numeric
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SPOTS,
            'Elevaciones puntuales (picos y depresiones)'
        ))

    def processAlgorithm(self, parameters, context, feedback):

        # ── Verificar matplotlib ──────────────────────────────────
        try:
            import matplotlib.path as mpath
        except ImportError:
            raise QgsProcessingException(
                "Se requiere 'matplotlib'. Instalalo con:\n"
                "  python -m pip install matplotlib"
            )

        # ── Leer MDE ──────────────────────────────────────────────
        rlayer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if rlayer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))

        raster_path = rlayer.dataProvider().dataSourceUri()
        feedback.pushInfo('Abriendo MDE...')
        ds = gdal.Open(raster_path)
        if ds is None:
            raise QgsProcessingException(f'No se pudo abrir el MDE: {raster_path}')

        banda      = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
        prj        = ds.GetProjection()
        gt         = ds.GetGeoTransform()
        ulx, xres, _, uly, _, yres = gt
        resol_X    = abs(xres)
        resol_Y    = abs(yres)
        nulo_val   = ds.GetRasterBand(1).GetNoDataValue()
        if nulo_val is not None:
            banda[banda == nulo_val] = np.nan
        rows_total = ds.RasterYSize
        cols_total = ds.RasterXSize
        ds = None

        crs_raster = QgsCoordinateReferenceSystem(prj)

        # ── Leer curvas de nivel ──────────────────────────────────
        curvas = self.parameterAsSource(parameters, self.CONTOURS, context)
        if curvas is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.CONTOURS))

        campo    = self.parameterAsFields(parameters, self.FIELD, context)
        campo_id = curvas.fields().indexFromName(campo[0])

        # Transformacion CRS si es necesario
        crs_curvas = curvas.sourceCrs()
        necesita_tr = crs_curvas != crs_raster
        if necesita_tr:
            tr = QgsCoordinateTransform(crs_curvas, crs_raster, QgsProject.instance())

        # ── Campos de salida ──────────────────────────────────────
        fields = QgsFields()
        fields.append(QgsField('elevacion', QMetaType.Double))
        fields.append(QgsField('tipo',      QMetaType.Int))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.SPOTS, context,
            fields, QgsWkbTypes.Point, crs_raster
        )
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.SPOTS))

        # ── Paso 1: recopilar anillos cerrados ────────────────────
        feedback.pushInfo('Detectando curvas cerradas (anillos)...')
        anillos = []   # lista de (coords, cota, punto_inicio)
        puntos_inicio = []

        for feat in curvas.getFeatures():
            geom = feat.geometry()
            if necesita_tr:
                geom.transform(tr)

            coords = (geom.asMultiPolyline()[0]
                      if geom.isMultipart()
                      else geom.asPolyline())

            # Curva cerrada: primer == ultimo vertice y al menos 4 vertices
            if len(coords) > 4 and coords[0] == coords[-1]:
                cota = float(feat[campo_id]) if feat[campo_id] is not None else None
                if cota is not None:
                    anillos.append(coords)
                    puntos_inicio.append(coords[0])
                    from types import SimpleNamespace
                    anillos[-1] = SimpleNamespace(
                        coords=coords,
                        cota=cota,
                        pt0=coords[0]
                    )

        if not anillos:
            feedback.pushWarning(
                'No se encontraron curvas cerradas. '
                'Asegurate de que la capa tenga curvas que formen anillos '
                '(primer vertice == ultimo vertice).'
            )
            return {self.SPOTS: dest_id}

        feedback.pushInfo(f'Anillos encontrados: {len(anillos)}')

        # ── Paso 2: filtrar anillos mas internos ──────────────────
        # (eliminar los que contienen a otros: solo procesar el innermost)
        feedback.pushInfo('Filtrando anillos mas internos...')
        pts_inicio = [a.pt0 for a in anillos]
        anillos_internos = []

        for i, a in enumerate(anillos):
            if feedback.isCanceled():
                break
            poly_geom = QgsGeometry.fromPolygonXY([a.coords])
            es_interno = True
            for j, pt in enumerate(pts_inicio):
                if i == j:
                    continue
                pt_geom = QgsGeometry.fromPointXY(pt)
                if poly_geom.contains(pt_geom):
                    es_interno = False
                    break
            if es_interno:
                anillos_internos.append(a)

        feedback.pushInfo(f'Anillos internos a procesar: {len(anillos_internos)}')

        # ── Paso 3: para cada anillo, encontrar pico o depresion ──
        total = 100.0 / len(anillos_internos) if anillos_internos else 0

        for idx, a in enumerate(anillos_internos):
            if feedback.isCanceled():
                break

            coords  = a.coords
            cota    = a.cota

            # Bounding box en coordenadas de pixel
            lins = [(uly - pt.y()) / resol_Y for pt in coords]
            cols = [(pt.x() - ulx)  / resol_X for pt in coords]

            lin_min = max(0,           int(np.floor(min(lins))))
            lin_max = min(rows_total-1, int(np.floor(max(lins))))
            col_min = max(0,           int(np.floor(min(cols))))
            col_max = min(cols_total-1, int(np.floor(max(cols))))

            nx = lin_max - lin_min + 1
            ny = col_max - col_min + 1

            if nx <= 0 or ny <= 0:
                continue

            # Path para mascara
            caminho = [(lin, col) for lin, col in zip(lins, cols)]
            path    = mpath.Path(caminho)

            # Grid de centros de pixeles
            lin_arr = np.arange(lin_min, lin_max + 1) + 0.5
            col_arr = np.arange(col_min, col_max + 1) + 0.5
            COL, LIN = np.meshgrid(col_arr, lin_arr)
            pts_grid = np.column_stack([LIN.ravel(), COL.ravel()])
            dentro   = path.contains_points(pts_grid).reshape(nx, ny)

            # Recorte del MDE
            recorte_mde = banda[lin_min:lin_max+1, col_min:col_max+1]
            if recorte_mde.shape != (nx, ny):
                dentro = dentro[:recorte_mde.shape[0], :recorte_mde.shape[1]]

            valores = np.where(dentro, recorte_mde, np.nan)
            media   = np.nanmean(valores)

            if np.isnan(media):
                continue

            # Decidir pico o depresion
            if media > cota:
                # Pico: buscar maximo
                tipo = 1
                idx_flat = np.nanargmax(valores)
                val_px   = np.nanmax(valores)
            else:
                # Depresion: buscar minimo
                tipo = -1
                idx_flat = np.nanargmin(valores)
                val_px   = np.nanmin(valores)

            r_local, c_local = np.unravel_index(idx_flat, valores.shape)

            # Coordenadas geograficas del centro del pixel
            X = ulx + resol_X * (col_min + c_local + 0.5)
            Y = uly - resol_Y * (lin_min + r_local + 0.5)

            feat_out = QgsFeature(fields)
            feat_out.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X, Y)))
            feat_out.setAttributes([round(float(val_px), 3), tipo])
            sink.addFeature(feat_out, QgsFeatureSink.FastInsert)

            feedback.setProgress(int((idx + 1) * total))

        feedback.pushInfo('Elevaciones puntuales generadas correctamente.')
        return {self.SPOTS: dest_id}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
