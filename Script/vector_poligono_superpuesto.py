# -*- coding: utf-8 -*-
"""
vector_poligono_superpuesto.py
Detecta superposiciones entre polígonos de la misma capa.
Soporta multiparte: convierte automáticamente a monoparte antes de procesar.
Adaptado de LFTools para Geomaticape.
"""
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterFeatureSink,
    QgsSpatialIndex, QgsFeatureRequest,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsWkbTypes, QgsFeatureSink,
)
import os


class VectorPoligonoSuperpuesto(QgsProcessingAlgorithm):
    INPUT  = 'INPUT'
    OUTPUT = 'OUTPUT'

    def createInstance(self): return VectorPoligonoSuperpuesto()
    def name(self): return 'vector_poligono_superpuesto'
    def displayName(self): return 'Polígono superpuesto propio'
    def group(self): return 'Vector'
    def groupId(self): return 'vector'
    def tags(self): return ['superposicion','overlap','poligono','solapamiento',
                            'multiparte','monoparte','topologia','validacion']
    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  'Icons','poligonos_tabla.png'))
    def shortHelpString(self):
        return (
            '<b>Polígono superpuesto propio</b><br>'
            'Detecta y extrae las áreas de superposición entre polígonos '
            'de una misma capa vectorial.<br><br>'
            '<b>Soporta capas multipartes:</b> convierte automáticamente '
            'cada parte a monoparte antes de analizar.<br><br>'
            'La salida contiene los atributos <b>ID1</b> e <b>ID2</b> '
            'identificando el par de polígonos que se superpone.<br><br>'
            'Útil para validación topológica de catastro, coberturas, '
            'parcelas y cualquier capa poligonal sin traslapes.<br><br>'
            'Adaptado de LFTools - Leandro França.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, 'Capa de polígonos',
            [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, 'Superposiciones detectadas'))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.INPUT))

        fields = QgsFields()
        fields.append(QgsField('ID1',     QMetaType.Int))
        fields.append(QgsField('ID2',     QMetaType.Int))
        fields.append(QgsField('area_m2', QMetaType.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, QgsWkbTypes.Polygon, source.sourceCrs())
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT))

        # ── Explotar a monoparte ──────────────────────────────────
        feedback.pushInfo('Convirtiendo multiparte a monoparte...')
        monopolygons = []   # lista de (orig_id, QgsGeometry monoparte)

        for feat in source.getFeatures():
            geom = feat.geometry()
            orig_id = feat.id()
            if geom.isMultipart():
                partes = geom.asMultiPolygon()
                for parte in partes:
                    g = QgsGeometry.fromPolygonXY(parte)
                    if g and not g.isEmpty():
                        monopolygons.append((orig_id, g))
            else:
                monopolygons.append((orig_id, QgsGeometry(geom)))

        n = len(monopolygons)
        feedback.pushInfo(f'Monoparte resultante: {n} polígonos')
        if n == 0:
            return {self.OUTPUT: dest_id}

        # ── Índice espacial sobre monopartes ──────────────────────
        feedback.pushInfo('Construyendo índice espacial...')
        idx = QgsSpatialIndex()
        for i, (oid, g) in enumerate(monopolygons):
            f = QgsFeature()
            f.setId(i)
            f.setGeometry(g)
            idx.insertFeature(f)

        # ── Detección de superposiciones ──────────────────────────
        feedback.pushInfo('Detectando superposiciones...')
        pares_vistos = set()
        total = 100.0 / n if n else 0

        for i, (oid1, g1) in enumerate(monopolygons):
            if feedback.isCanceled():
                break
            candidatos = idx.intersects(g1.boundingBox())
            for j in candidatos:
                if j <= i:
                    continue
                par = (i, j)
                if par in pares_vistos:
                    continue
                pares_vistos.add(par)

                oid2, g2 = monopolygons[j]
                if not g1.intersects(g2):
                    continue

                inter = g1.intersection(g2)
                if inter.isEmpty():
                    continue

                # Extraer solo partes poligonales
                col = (inter.asGeometryCollection()
                       if inter.isMultipart() else [inter])
                for parte in col:
                    if parte.type() != QgsWkbTypes.PolygonGeometry:
                        continue
                    area = parte.area()
                    if area < 1e-10:
                        continue
                    polys = (parte.asMultiPolygon()
                             if parte.isMultipart()
                             else [parte.asPolygon()])
                    for poly in polys:
                        f_out = QgsFeature(fields)
                        f_out.setGeometry(QgsGeometry.fromPolygonXY(poly))
                        f_out.setAttributes([oid1, oid2, round(area, 4)])
                        sink.addFeature(f_out, QgsFeatureSink.FastInsert)

            feedback.setProgress(int((i + 1) * total))

        feedback.pushInfo(f'Pares con superposición: {len(pares_vistos)}')
        feedback.pushInfo('Proceso completado.')
        return {self.OUTPUT: dest_id}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
