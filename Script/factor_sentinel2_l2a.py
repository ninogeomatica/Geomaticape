import os
import tempfile
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterRasterDestination
)
from qgis import processing
from osgeo import gdal


class FactorSentinel2L2A(QgsProcessingAlgorithm):

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_MULTISPECTRAL = "OUTPUT_MULTISPECTRAL"

    def name(self):
        return "factor_sentinel2_l2a"

    def displayName(self):
        return "Factor de escala Sentinel-2 L2A (10m)"

    def group(self):
        return "Conversion"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.png'))

    def groupId(self):
        return "geomaticape_conversion"

    def createInstance(self):
        return FactorSentinel2L2A()
    # -----------------------------
    # PANEL DE AYUDA
    # -----------------------------

    def shortHelpString(self):
        return """
<h3>Factor de escala Sentinel-2 Level-2A</h3>

<b>Autor:</b> GEOMATICA AMBIENTAL <br>
<b>Plugin:</b> Geomaticape<br><br>
<b>Versión:</b> 1.2
<b>Descripción:</b>
Esta herramienta aplica el factor de escala (0.0001) a las bandas 
de Sentinel-2 Level-2A para obtener reflectancia real y genera una 
imagen multiespectral remuestreada a 10 metros.
<b>Proceso:</b>
<ul>
<li> - Detecta producto Sentinel-2 L2A (IMG_DATA)</li>
<li> - Localiza carpetas R10m, R20m y R60m</li>
<li> - Aplica factor de escala: DN × 0.0001</li>
<li> - Remuestrea bandas 20m y 60m a 10m</li>
<li> - Genera raster multibanda final</li>
<li> - Elimina archivos temporales automáticamente</li>
</ul>
<b>Bandas utilizadas:</b>
B01, B02, B03, B04, B05, B06, B07, B08, B11, B12
<b>Requisito:</b> Producto Sentinel-2 MSIL2A.
<b>Web:</b> https://www.geomatica.pe/
"""
    # -----------------------------
    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_FOLDER,
                "Carpeta Sentinel-2 Level-2A (IMG_DATA)",
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

        if "MSIL1C" in folder.upper():
            raise Exception("Este proceso es solo para Sentinel-2 Level-2A.")

        if "MSIL2A" not in folder.upper():
            raise Exception("No se detecta producto Sentinel-2 L2A válido.")

        img_data_folder = None
        for root, dirs, files in os.walk(folder):
            if "IMG_DATA" in root.upper():
                img_data_folder = root
                break

        if not img_data_folder:
            raise Exception("No se encontró carpeta IMG_DATA.")

        r10 = os.path.join(img_data_folder, "R10m")
        r20 = os.path.join(img_data_folder, "R20m")
        r60 = os.path.join(img_data_folder, "R60m")

        # Diccionario banda → carpeta y sufijo correcto
        bandas_info = {
            "B01": (r60, "_B01_60M.JP2"),
            "B02": (r10, "_B02_10M.JP2"),
            "B03": (r10, "_B03_10M.JP2"),
            "B04": (r10, "_B04_10M.JP2"),
            "B05": (r20, "_B05_20M.JP2"),
            "B06": (r20, "_B06_20M.JP2"),
            "B07": (r20, "_B07_20M.JP2"),
            "B08": (r10, "_B08_10M.JP2"),
            "B11": (r20, "_B11_20M.JP2"),
            "B12": (r20, "_B12_20M.JP2"),
        }

        bandas_procesadas = []
        temporales = []
        temp_dir = tempfile.mkdtemp()

        for banda, (carpeta, sufijo) in bandas_info.items():

            if not os.path.exists(carpeta):
                raise Exception(f"No existe carpeta {carpeta}")

            archivos = os.listdir(carpeta)

            archivo_encontrado = None
            for archivo in archivos:
                if archivo.upper().endswith(sufijo):
                    archivo_encontrado = archivo
                    break

            if not archivo_encontrado:
                raise Exception(f"No se encontró {sufijo}")

            ruta = os.path.join(carpeta, archivo_encontrado)
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
