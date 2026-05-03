# -*- coding: utf-8 -*-
"""
mde_curvas_nivel_intermedias.py
Genera curvas de nivel desde un MDE raster y clasifica cada curva
como 'normal' (intermedia) o 'maestra' según el patrón cartográfico
estándar (cada N curvas una maestra).
Campo de salida: tipo_curva = 'normal' | 'maestra'
Geomaticape v1.24 - Geomatica Ambiental
"""
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsFeature, QgsField, QgsFields, QgsWkbTypes, QgsFeatureSink,
    QgsVectorLayer, QgsCoordinateReferenceSystem,
)
import processing as qgs_processing
import os
import math


class MDECurvasNivelIntermedias(QgsProcessingAlgorithm):
    DEM           = 'DEM'
    EQUIDISTANCIA = 'EQUIDISTANCIA'
    MULTIPLICADOR = 'MULTIPLICADOR'
    OUTPUT        = 'OUTPUT'

    def createInstance(self): return MDECurvasNivelIntermedias()
    def name(self): return 'mde_curvas_nivel_intermedias'
    def displayName(self): return 'Generar curvas de nivel intermedias'
    def group(self): return 'MDE'
    def groupId(self): return 'mde_geo'
    def tags(self): return ['curvas','nivel','intermedias','maestras','mde','dem',
                            'contour','isohipsa','altimetria','cartografia','equidistancia']
    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  'Icons','extraer_valores.png'))
    def shortHelpString(self):
        return (
            '<b>Extraer curvas de nivel intermedias</b><br>'
            'Genera curvas de nivel directamente desde un MDE raster y clasifica '
            'cada curva según el patrón cartográfico estándar:<br><br>'
            '<table border="1" cellpadding="4" style="border-collapse:collapse">'
            '<tr style="background:#ddd"><th>Curva</th><th>tipo_curva</th></tr>'
            '<tr><td>1ª, 2ª, 3ª, 4ª</td><td><b>normal</b></td></tr>'
            '<tr><td>5ª (× multiplicador)</td><td><b>maestra</b></td></tr>'
            '</table><br>'
            '<b>Ejemplo — equidistancia 10 m, multiplicador 5:</b><br>'
            '<ul>'
            '<li>10, 20, 30, 40 m → <b>normal</b></li>'
            '<li>50 m → <b>maestra</b></li>'
            '<li>60, 70, 80, 90 m → <b>normal</b></li>'
            '<li>100 m → <b>maestra</b></li>'
            '</ul>'
            '<b>Parámetros:</b><br>'
            '<ul>'
            '<li><b>MDE:</b> raster de elevación de entrada.</li>'
            '<li><b>Equidistancia (m):</b> intervalo entre curvas consecutivas.</li>'
            '<li><b>Multiplicador maestras:</b> cada cuántas curvas se genera '
            'una maestra (por defecto 5).</li>'
            '</ul>'
            '<b>Salida:</b> capa de líneas con campos <i>cota</i> (valor de '
            'elevación) y <i>tipo_curva</i> (texto: "normal" o "maestra").<br><br>'
            'Geomaticape v1.24.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DEM, 'MDE (Modelo Digital de Elevación)',
            [QgsProcessing.TypeRaster]))
        self.addParameter(QgsProcessingParameterNumber(
            self.EQUIDISTANCIA,
            'Equidistancia entre curvas (m)',
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=10.0, minValue=0.001))
        self.addParameter(QgsProcessingParameterNumber(
            self.MULTIPLICADOR,
            'Multiplicador de curvas maestras (cada N curvas)',
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=5, minValue=2, maxValue=20))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, 'Curvas de nivel clasificadas'))

    def processAlgorithm(self, parameters, context, feedback):
        dem_layer   = self.parameterAsRasterLayer(parameters, self.DEM, context)
        if dem_layer is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.DEM))

        equidist     = self.parameterAsDouble(parameters, self.EQUIDISTANCIA, context)
        multiplicador = self.parameterAsInt(parameters,  self.MULTIPLICADOR,  context)
        equidist_mae  = equidist * multiplicador

        feedback.pushInfo('─' * 52)
        feedback.pushInfo(f'Equidistancia normal  : {equidist} m')
        feedback.pushInfo(f'Multiplicador         : {multiplicador}')
        feedback.pushInfo(f'Equidistancia maestra : {equidist_mae} m')
        feedback.pushInfo('─' * 52)

        # ── Paso 1: generar curvas de nivel con gdal:contour ──────
        feedback.pushInfo('Generando curvas de nivel con GDAL Contour...')
        resultado = qgs_processing.run(
            'gdal:contour',
            {
                'INPUT'       : dem_layer,
                'BAND'        : 1,
                'INTERVAL'    : equidist,
                'FIELD_NAME'  : 'cota',
                'CREATE_3D'   : False,
                'IGNORE_NODATA': True,
                'NODATA'      : None,
                'OFFSET'      : 0,
                'EXTRA'       : '',
                'OUTPUT'      : 'TEMPORARY_OUTPUT',
            },
            context=context, feedback=feedback
        )
        curvas_layer = resultado['OUTPUT']

        if isinstance(curvas_layer, str):
            curvas_layer = QgsVectorLayer(curvas_layer, 'curvas_tmp', 'ogr')
        if not curvas_layer or not curvas_layer.isValid():
            raise QgsProcessingException(
                'GDAL Contour no generó una capa válida. '
                'Verifica que el MDE tenga datos de elevación válidos.')

        n_curvas = curvas_layer.featureCount()
        feedback.pushInfo(f'Curvas generadas: {n_curvas}')

        # ── Paso 2: preparar campos de salida ─────────────────────
        fields = QgsFields()
        fields.append(QgsField('cota',       QMetaType.Double))
        fields.append(QgsField('tipo_curva', QMetaType.QString))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, QgsWkbTypes.LineString, dem_layer.crs())
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT))

        # ── Paso 3: clasificar y escribir ─────────────────────────
        feedback.pushInfo('Clasificando curvas...')
        idx_cota = curvas_layer.fields().indexFromName('cota')
        if idx_cota < 0:
            # Intentar campo 'ELEV' (nombre alternativo de GDAL)
            idx_cota = curvas_layer.fields().indexFromName('ELEV')
        if idx_cota < 0:
            raise QgsProcessingException(
                "No se encontró el campo de cota ('cota' o 'ELEV') "
                "en la capa generada por GDAL Contour.")

        total      = 100.0 / n_curvas if n_curvas else 0
        n_maestras = 0
        n_normales = 0
        tol        = equidist * 0.01   # tolerancia para modulo flotante

        for current, feat in enumerate(curvas_layer.getFeatures()):
            if feedback.isCanceled():
                break

            cota_raw = feat[idx_cota]
            if cota_raw is None:
                feedback.setProgress(int((current + 1) * total))
                continue

            cota = float(cota_raw)

            # Normalizar al múltiplo más cercano de equidist
            cota_norm = round(cota / equidist) * equidist

            # Clasificar
            residuo    = abs(cota_norm % equidist_mae)
            es_maestra = (residuo < tol or
                          abs(residuo - equidist_mae) < tol)
            tipo = 'maestra' if es_maestra else 'normal'

            f_out = QgsFeature(fields)
            f_out.setGeometry(feat.geometry())
            f_out.setAttributes([round(cota_norm, 4), tipo])
            sink.addFeature(f_out, QgsFeatureSink.FastInsert)

            if es_maestra:
                n_maestras += 1
            else:
                n_normales += 1

            feedback.setProgress(int((current + 1) * total))

        feedback.pushInfo('─' * 52)
        feedback.pushInfo(f'Curvas normales  : {n_normales}')
        feedback.pushInfo(f'Curvas maestras  : {n_maestras}')
        feedback.pushInfo('Clasificación completada correctamente.')
        feedback.pushInfo('─' * 52)

        return {self.OUTPUT: dest_id}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
