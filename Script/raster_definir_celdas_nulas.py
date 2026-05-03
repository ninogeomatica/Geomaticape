# -*- coding: utf-8 -*-
"""
raster_definir_celdas_nulas.py
Define píxeles nulos en un raster según un rango de valores válidos.
Adaptado de LFTools (Leandro França) para Geomaticape.
"""

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProject,
    QgsRasterLayer,
)
from osgeo import gdal, osr
import numpy as np
import os


class RasterDefinirCeldasNulas(QgsProcessingAlgorithm):

    RASTER_IN = 'RASTER_IN'
    MIN = 'MIN'
    MAX = 'MAX'
    NULLVALUE = 'NULLVALUE'
    OPEN = 'OPEN'
    RASTER_OUT = 'RASTER_OUT'

    def createInstance(self):
        return RasterDefinirCeldasNulas()

    def name(self):
        return 'raster_definir_celdas_nulas'

    def displayName(self):
        return 'Definir celdas nulas'

    def group(self):
        return 'Ráster'

    def groupId(self):
        return 'raster_geo'

    def tags(self):
        return ['nulo', 'nodata', 'mascara', 'pixel', 'rango', 'raster',
                'umbral', 'threshold', 'null', 'transparente']

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  'Icons', 'indices.png'))

    def shortHelpString(self):
        return (
            '<b>Definir celdas nulas</b><br>'
            'Establece como valor nulo (NoData) todos los píxeles que estén '
            '<b>fuera</b> del rango [mínimo, máximo] definido.<br><br>'
            '<b>Uso típico:</b><br>'
            '- Enmascarar valores de fondo (0 o -9999) en imágenes satelitales.<br>'
            '- Limpiar artefactos fuera del área de interés.<br>'
            '- Preparar imágenes para mosaiqueo.<br><br>'
            '<b>Ejemplo:</b> Min=1, Max=65535, Nulo=0 → todos los píxeles con valor 0 '
            'serán marcados como NoData.<br><br>'
            'Adaptado de LFTools - Leandro França.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.RASTER_IN,
                'Raster de entrada',
                [QgsProcessing.TypeRaster]
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN,
                'Valor mínimo válido',
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=1
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX,
                'Valor máximo válido',
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=65535
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.NULLVALUE,
                'Valor a asignar como nulo (NoData)',
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.OPEN,
                'Cargar resultado en el proyecto',
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.RASTER_OUT,
                'Raster con celdas nulas definidas',
                fileFilter='GeoTIFF (*.tif)'
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        rlayer = self.parameterAsRasterLayer(parameters, self.RASTER_IN, context)
        if rlayer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.RASTER_IN))

        raster_path = rlayer.dataProvider().dataSourceUri()
        output = self.parameterAsFileOutput(parameters, self.RASTER_OUT, context)
        carregar = self.parameterAsBool(parameters, self.OPEN, context)
        vmin = self.parameterAsDouble(parameters, self.MIN, context)
        vmax = self.parameterAsDouble(parameters, self.MAX, context)
        nulo = self.parameterAsDouble(parameters, self.NULLVALUE, context)

        if vmax < vmin:
            raise QgsProcessingException(
                'El valor máximo debe ser mayor o igual al valor mínimo.')
        if vmin < nulo < vmax:
            raise QgsProcessingException(
                'El valor nulo no puede estar dentro del rango válido [mínimo, máximo].')

        ds = gdal.Open(raster_path)
        prj = ds.GetProjection()
        geotransform = ds.GetGeoTransform()
        GDT = ds.GetRasterBand(1).DataType
        n_bands = ds.RasterCount
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        CRS = osr.SpatialReference(wkt=prj)

        Driver = gdal.GetDriverByName('GTiff').Create(output, cols, rows, n_bands, GDT)
        Driver.SetGeoTransform(geotransform)
        Driver.SetProjection(CRS.ExportToWkt())

        for k in range(n_bands):
            feedback.pushInfo(f'Procesando banda {k+1}/{n_bands}...')
            banda = ds.GetRasterBand(k+1).ReadAsArray().astype(np.float64)
            # Píxeles fuera del rango → nulo
            banda = np.where((banda >= vmin) & (banda <= vmax), banda, nulo)
            out_b = Driver.GetRasterBand(k + 1)
            out_b.WriteArray(banda)
            out_b.SetNoDataValue(nulo)
            feedback.setProgress(int((k + 1) / n_bands * 100))

        ds = None
        Driver.FlushCache()
        Driver = None

        feedback.pushInfo('Celdas nulas definidas correctamente.')
        self._output = output
        self._carregar = carregar
        return {self.RASTER_OUT: output}

    def postProcessAlgorithm(self, context, feedback):
        if self._carregar:
            rlayer = QgsRasterLayer(self._output, 'Raster - celdas nulas')
            QgsProject.instance().addMapLayer(rlayer)
        return {}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
