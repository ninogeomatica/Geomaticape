# -*- coding: utf-8 -*-
"""
vector_sucesion_cruzada.py  →  Secciones transversales
Genera secciones transversales perpendiculares a una capa de líneas.
Puerto directo de LFTools Vect_CrossSections (Leandro França) para Geomaticape.
"""
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsWkbTypes, QgsFeatureSink,
)
from numpy import array, arange, sqrt, floor
from numpy.linalg import norm
import os
import math


def _metros_a_grados(metros, lat_media_deg):
    """Convierte metros a grados decimales en la latitud dada."""
    lat_rad = math.radians(lat_media_deg)
    R = 6378137.0
    grados_lon = metros / (R * math.cos(lat_rad)) * (180.0 / math.pi)
    return grados_lon


class VectorSucesionCruzada(QgsProcessingAlgorithm):
    LINES        = 'LINES'
    LONGITUDINAL = 'LONGITUDINAL'
    TRANVERSE    = 'TRANVERSE'
    OUTPUT       = 'OUTPUT'

    def createInstance(self): return VectorSucesionCruzada()
    def name(self): return 'vector_sucesion_cruzada'
    def displayName(self): return 'Secciones transversales'
    def group(self): return 'Vector'
    def groupId(self): return 'vector'
    def tags(self): return ['seccion','transversal','perfil','linea','perpendicular',
                            'distancia','topografia','carretera','drenaje','cross']
    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  'Icons','poligonos_tabla.png'))
    def shortHelpString(self):
        return (
            '<b>Secciones transversales</b><br>'
            'Genera secciones transversales (perpendiculares) a lo largo de '
            'una capa de líneas.<br><br>'
            '<b>Parámetros:</b><br>'
            '<ul>'
            '<li><b>Distancia longitudinal (m):</b> espaciado entre secciones '
            'a lo largo de la línea.</li>'
            '<li><b>Distancia transversal (m):</b> longitud total de cada '
            'sección (mitad a cada lado de la línea).</li>'
            '</ul>'
            '<b>Atributos de salida:</b> heredados de la línea original más '
            '<i>feat_id</i>, <i>secuencia</i> y <i>distancia</i> (m desde el inicio).<br><br>'
            'Soporta CRS geográficos y proyectados.<br><br>'
            'Puerto de LFTools CrossSections - Leandro França.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LINES, 'Capa de líneas',
            [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterNumber(
            self.LONGITUDINAL,
            'Distancia longitudinal — espaciado entre secciones (m)',
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=50.0, minValue=0.001))
        self.addParameter(QgsProcessingParameterNumber(
            self.TRANVERSE,
            'Distancia transversal — longitud total de cada sección (m)',
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=150.0, minValue=0.001))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, 'Secciones transversales'))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsSource(parameters, self.LINES, context)
        if layer is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.LINES))
        SRC = layer.sourceCrs()

        # ── Campos de salida ──────────────────────────────────────
        Fields     = QgsFields(layer.fields())
        fieldnames = [f.name() for f in Fields]

        nuevos = [
            ('feat_id',    QMetaType.Int),
            ('secuencia',  QMetaType.Int),
            ('distancia',  QMetaType.Double),
        ]
        for nombre, tipo in nuevos:
            fn = nombre
            cont = 0
            while fn in fieldnames:
                cont += 1
                fn = f'{nombre}_{cont}'
            Fields.append(QgsField(fn, tipo))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            Fields, QgsWkbTypes.LineString, SRC)
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT))

        distSec = self.parameterAsDouble(parameters, self.LONGITUDINAL, context)
        tamSec  = self.parameterAsDouble(parameters, self.TRANVERSE,    context)

        # Ajuste a grados si CRS geográfico
        if SRC.isGeographic():
            ext    = layer.sourceExtent()
            lat_m  = (ext.yMaximum() + ext.yMinimum()) / 2.0
            distSec = _metros_a_grados(distSec, lat_m)
            tamSec  = _metros_a_grados(tamSec,  lat_m)

        def dist2d(P1, P2):
            return sqrt((P1.x()-P2.x())**2 + (P1.y()-P2.y())**2)

        total = 100.0 / layer.featureCount() if layer.featureCount() > 0 else 0

        for current, feat in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                break

            geom = feat.geometry()
            comprimento = geom.length()
            coord = (geom.asMultiPolyline()[0]
                     if geom.isMultipart() else geom.asPolyline())

            # Distancias acumuladas en cada vértice
            ListaDist = [0.0]
            soma = 0.0
            for i in range(len(coord) - 1):
                soma += dist2d(coord[i], coord[i+1])
                ListaDist.append(soma)

            # Posiciones de las secciones a lo largo de la línea
            if distSec < comprimento:
                NumSec      = int(floor(comprimento / distSec))
                DistSecNova = comprimento / NumSec
            else:
                NumSec      = 1
                DistSecNova = comprimento
            dist = arange(0, comprimento + DistSecNova, DistSecNova)

            LIST_COORD = []
            LIST_ATT   = []
            cont = 0

            for k in range(len(coord) - 1):
                while (cont < len(dist) and
                       ListaDist[k] <= dist[cont] < ListaDist[k+1]):
                    p1v = array([coord[k].x(),   coord[k].y()])
                    p2v = array([coord[k+1].x(), coord[k+1].y()])
                    vetor = p2v - p1v
                    vetor /= norm(vetor)
                    centro = p1v + vetor * (dist[cont] - ListaDist[k])

                    # Puntos extremos perpendiculares
                    pA = centro + array([ vetor[1], -vetor[0]]) * tamSec / 2.0
                    pB = centro + array([-vetor[1],  vetor[0]]) * tamSec / 2.0
                    LIST_COORD.append([
                        QgsPointXY(float(pA[0]), float(pA[1])),
                        QgsPointXY(float(centro[0]), float(centro[1])),
                        QgsPointXY(float(pB[0]), float(pB[1])),
                    ])
                    LIST_ATT.append(
                        feat.attributes() + [feat.id(), cont + 1,
                                             float(dist[cont])])
                    cont += 1
                    if cont > NumSec:
                        break
                if cont > NumSec:
                    break

            # Última sección (en el extremo final de la línea)
            cont += 1
            p1v   = array([coord[-2].x(), coord[-2].y()])
            p2v   = array([coord[-1].x(), coord[-1].y()])
            vetor = p2v - p1v
            vetor /= norm(vetor)
            centro = array([coord[-1].x(), coord[-1].y()])
            pA = centro + array([ vetor[1], -vetor[0]]) * tamSec / 2.0
            pB = centro + array([-vetor[1],  vetor[0]]) * tamSec / 2.0
            LIST_COORD.append([
                QgsPointXY(float(pA[0]), float(pA[1])),
                QgsPointXY(float(centro[0]), float(centro[1])),
                QgsPointXY(float(pB[0]), float(pB[1])),
            ])
            LIST_ATT.append(feat.attributes() + [feat.id(), cont,
                                                  float(comprimento)])

            # Guardar features
            f_out = QgsFeature()
            for coords_sec, atts in zip(LIST_COORD, LIST_ATT):
                f_out.setGeometry(QgsGeometry.fromPolylineXY(coords_sec))
                f_out.setAttributes(atts)
                sink.addFeature(f_out, QgsFeatureSink.FastInsert)

            feedback.setProgress(int((current + 1) * total))

        feedback.pushInfo('Secciones transversales generadas correctamente.')
        return {self.OUTPUT: dest_id}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
