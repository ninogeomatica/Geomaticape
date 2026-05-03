import os
import tempfile
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterRasterDestination
)
from qgis import processing
from osgeo import gdal


class FactorSentinel2L1A(QgsProcessingAlgorithm):

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_MULTISPECTRAL = "OUTPUT_MULTISPECTRAL"

    def name(self):
        return "factor_sentinel2"

    def displayName(self):
        return "Factor de escala Sentinel-2 L1C (10m)"

    def group(self):
        return "Conversion"

    def groupId(self):
        return "geomaticape_conversion"
    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.png'))

    def createInstance(self):
        return FactorSentinel2L1A()

        # -----------------------------
    # PANEL DE AYUDA
    # -----------------------------

    def shortHelpString(self):
        return """
<h3>Factor de escala Sentinel-2 Level-1C</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL
<b>Plugin:</b> Geomaticape<br>
<b>Versión:</b> 1.2
<b>Descripción:</b>
Esta herramienta aplica el factor de escala (0.0001) a las bandas 
de Sentinel-2 Level-1C para convertir los valores digitales (DN) 
en reflectancia TOA y genera una imagen multiespectral 
remuestreada a 10 metros.
<b>Proceso:</b>
<ul>
<li> ✔ Detecta producto Sentinel-2 L1C (IMG_DATA)</li>
<li> ✔ Aplica factor de escala: DN × 0.0001</li>
<li> ✔ Remuestrea bandas 20m y 60m a 10m</li>
<li> ✔ Genera raster multibanda a 10 metros</li>
<li> ✔ Elimina archivos temporales automáticamente</li>
</ul>
<b>Bandas utilizadas:</b>
B01, B02, B03, B04, B05, B06, B07, B08, B11, B12
<b>Requisito:</b> Producto Sentinel-2 MSIL1C
<b>Web:</b> https://www.geomatica.pe/
"""
    # -----------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_FOLDER,
                "Carpeta Sentinel-2 Level-1C (IMG_DATA)",
                behavior=QgsProcessingParameterFile.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_MULTISPECTRAL,
                "Imagen multiespectral 10m"
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsString(parameters, self.INPUT_FOLDER, context)
        output = self.parameterAsOutputLayer(parameters, self.OUTPUT_MULTISPECTRAL, context)

        # Validar L1C
        if "MSIL2A" in folder.upper():
            raise Exception("Este proceso es solo para Sentinel-2 Level-1C.")

        if "MSIL1C" not in folder.upper():
            raise Exception("No se detecta producto Sentinel-2 L1C válido.")

        # Buscar IMG_DATA
        img_data_folder = None
        for root, dirs, files in os.walk(folder):
            if "IMG_DATA" in root.upper():
                img_data_folder = root
                break

        if not img_data_folder:
            raise Exception("No se encontró carpeta IMG_DATA.")

        bandas_orden = [
            "_B01.JP2",
            "_B02.JP2",
            "_B03.JP2",
            "_B04.JP2",
            "_B05.JP2",
            "_B06.JP2",
            "_B07.JP2",
            "_B08.JP2",
            "_B11.JP2",
            "_B12.JP2"
        ]

        archivos = os.listdir(img_data_folder)
        bandas_procesadas = []
        temporales = []
        temp_dir = tempfile.mkdtemp()

        for sufijo in bandas_orden:

            archivo_encontrado = None

            for archivo in archivos:
                if archivo.upper().endswith(sufijo):
                    archivo_encontrado = archivo
                    break

            if not archivo_encontrado:
                raise Exception(f"No se encontró banda {sufijo}")

            ruta = os.path.join(img_data_folder, archivo_encontrado)
            nombre_base = os.path.splitext(archivo_encontrado)[0]

            # Escalado reflectancia
            salida_scale = os.path.join(temp_dir, f"scaled_{nombre_base}.tif")

            processing.run(
                "gdal:rastercalculator",
                {
                    'INPUT_A': ruta,
                    'BAND_A': 1,
                    'FORMULA': '(A * 0.0001)',
                    'RTYPE': 5,
                    'OUTPUT': salida_scale
                },
                context=context,
                feedback=feedback
            )

            temporales.append(salida_scale)

            # Detectar resolución
            ds = gdal.Open(salida_scale)
            pixel_size = abs(ds.GetGeoTransform()[1])
            ds = None

            if pixel_size == 10:
                bandas_procesadas.append(salida_scale)
            else:
                salida_resample = os.path.join(temp_dir, f"resample_{nombre_base}.tif")

                processing.run(
                    "gdal:warpreproject",
                    {
                        'INPUT': salida_scale,
                        'TARGET_RESOLUTION': 10,
                        'RESAMPLING': 1,
                        'OUTPUT': salida_resample
                    },
                    context=context,
                    feedback=feedback
                )

                temporales.append(salida_resample)
                bandas_procesadas.append(salida_resample)

        # Merge final
        processing.run(
            "gdal:merge",
            {
                'INPUT': bandas_procesadas,
                'SEPARATE': True,
                'OUTPUT': output
            },
            context=context,
            feedback=feedback
        )

        # Asignar nombre interno bandas
        dataset = gdal.Open(output, gdal.GA_Update)

        band_names = ["Aerosol", "Blue", "Green", "Red", "Red_Edge1", "Red_Edge2", "Red_Edge3", "NIR", "SWIR1", "SWIR2"]

        for i, name in enumerate(band_names):
            band = dataset.GetRasterBand(i + 1)
            band.SetDescription(name)

        dataset = None

        # Liberar memoria antes de borrar
        del bandas_procesadas

        # Eliminar temporales
        for temp in temporales:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except:
                pass

        return {self.OUTPUT_MULTISPECTRAL: output}
    def run(self):
        processing.execAlgorithmDialog(self)
