# -*- coding: utf-8 -*-
"""
vector_angulo_poligono.py
Calcula ángulos internos y externos de los vértices de polígonos.
Salida: solo capa de puntos con ángulos.
Ángulo interno/externo verificado según signo del área (Shoelace).
Adaptado de LFTools para Geomaticape.
"""
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsWkbTypes, QgsFeatureSink,
)
from math import pi
import numpy as np
import os


def _azimute(A, B):
    dx = B.x() - A.x()
    dy = B.y() - A.y()
    return np.arctan2(dx, dy) % (2 * pi)


def _dd2dms(dd, dec=1):
    d = int(dd)
    m = int((dd - d) * 60)
    s = round(((dd - d) * 60 - m) * 60, dec)
    return f"{d}°{m:02d}'{s:04.1f}\""


def _area_shoelace(coords):
    """Área con signo (Gauss). Positivo = sentido antihorario."""
    n = len(coords)
    a = 0.0
    for i in range(n):
        j = (i + 1) % n
        a += coords[i].x() * coords[j].y()
        a -= coords[j].x() * coords[i].y()
    return a / 2.0


class VectorAnguloPoligono(QgsProcessingAlgorithm):
    POLYGONS = 'POLYGONS'
    FIELD    = 'FIELD'
    ANGLES   = 'ANGLES'

    def createInstance(self): return VectorAnguloPoligono()
    def name(self): return 'vector_angulo_poligono'
    def displayName(self): return 'Calcular ángulo de polígono'
    def group(self): return 'Vector'
    def groupId(self): return 'vector'
    def tags(self): return ['angulo','poligono','vertice','interior','exterior',
                            'topografia','medicion','gms','decimal']
    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  'Icons','poligonos_tabla.png'))
    def shortHelpString(self):
        return (
            '<b>Calcular ángulo de polígono</b><br>'
            'Calcula los ángulos internos y externos en cada vértice de una '
            'capa de polígonos.<br><br>'
            '<b>Salida:</b> capa de puntos con los campos:<br>'
            '<ul>'
            '<li><b>orden</b>: número de vértice en el anillo.</li>'
            '<li><b>ang_int_dec / ang_int_gms</b>: ángulo interno en decimal y GMS.</li>'
            '<li><b>ang_ext_dec / ang_ext_gms</b>: ángulo externo en decimal y GMS.</li>'
            '<li><b>feat_id</b>: ID de la feature original.</li>'
            '</ul>'
            '<b>Criterio interno/externo:</b> basado en el signo del área '
            '(Shoelace). Para anillos en sentido antihorario (estándar OGC) '
            'el ángulo "hacia adentro" es el ángulo interior del polígono.<br><br>'
            'Adaptado de LFTools - Leandro França.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POLYGONS, 'Capa de polígonos',
            [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD, 'Campo ID del polígono (opcional)',
            parentLayerParameterName=self.POLYGONS, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.ANGLES, 'Puntos con ángulos'))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.POLYGONS, context)
        if source is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.POLYGONS))

        campo    = self.parameterAsFields(parameters, self.FIELD, context)
        CRS      = source.sourceCrs()
        col_idx  = -1
        id_type  = QMetaType.Int
        if campo:
            col_idx = source.fields().indexFromName(campo[0])
            if col_idx >= 0:
                id_type = source.fields()[col_idx].type()

        fields = QgsFields()
        for nombre, tipo in [
            ('Orden',       QMetaType.Int),
            ('Ang_int_dec', QMetaType.Double),
            ('Ang_int_gms', QMetaType.QString),
            ('Ang_ext_dec', QMetaType.Double),
            ('Ang_ext_gms', QMetaType.QString),
            ('Feat_id',     id_type),
        ]:
            fields.append(QgsField(nombre, tipo))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.ANGLES, context,
            fields, QgsWkbTypes.Point, CRS)
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.ANGLES))

        total = 100.0 / source.featureCount() if source.featureCount() else 0

        for current, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break

            geom  = feat.geometry()
            partes = (geom.asMultiPolygon()
                      if geom.isMultipart() else [geom.asPolygon()])

            feat_id = (feat.id() if col_idx < 0
                       else feat[col_idx])

            for parte in partes:
                ring   = parte[0]          # anillo externo
                coords = ring[:-1]         # sin el vértice repetido final
                n      = len(coords)
                if n < 3:
                    continue

                # Área con signo para determinar orientación del anillo
                area = _area_shoelace(coords)
                # area > 0 → antihorario (OGC/QGIS estándar)
                # area < 0 → horario

                for k in range(n):
                    P1 = coords[(k - 1) % n]   # vértice anterior
                    P2 = coords[k]              # vértice actual
                    P3 = coords[(k + 1) % n]   # vértice siguiente

                    az1 = _azimute(P2, P1)      # azimut P2→P1
                    az2 = _azimute(P2, P3)      # azimut P2→P3

                    # Ángulo de giro de az1 a az2 en sentido antihorario
                    alfa = (az2 - az1) % (2 * pi)
                    alfa_deg = alfa * 180.0 / pi

                    # Para anillo antihorario (area > 0):
                    #   alfa_deg es el ángulo EXTERIOR
                    #   180 - alfa_deg ... no; lo correcto:
                    #   ángulo interior = 360 - alfa_deg  si alfa_deg > 180
                    #   ángulo interior = alfa_deg         si alfa_deg < 180
                    # Regla general según orientación:
                    if area > 0:
                        # Antihorario: alfa mide el giro exterior
                        ang_ext = alfa_deg
                        ang_int = 360.0 - alfa_deg
                    else:
                        # Horario: alfa mide el giro interior -Cambiado
                        #ang_int = alfa_deg
                        #ang_ext = 360.0 - alfa_deg
                        ang_ext = alfa_deg
                        ang_int = 360.0 - alfa_deg

                    # Normalizar a [0, 360]
                    ang_int = ang_int % 360.0
                    ang_ext = ang_ext % 360.0

                    f = QgsFeature(fields)
                    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(P2)))
                    f.setAttributes([
                        k + 1,
                        round(ang_int, 6), _dd2dms(ang_int),
                        round(ang_ext, 6), _dd2dms(ang_ext),
                        feat_id,
                    ])
                    sink.addFeature(f, QgsFeatureSink.FastInsert)

            feedback.setProgress(int((current + 1) * total))

        feedback.pushInfo('Ángulos calculados correctamente.')
        return {self.ANGLES: dest_id}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
