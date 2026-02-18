import os
import tempfile
import shutil
import gc
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterRasterDestination
)
from qgis import processing
from osgeo import gdal


class FactorLandsat(QgsProcessingAlgorithm):

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_MULTISPECTRAL = "OUTPUT_MULTISPECTRAL"
    OUTPUT_THERMAL = "OUTPUT_THERMAL"

    def name(self):
        return "factor_landsat"

    def displayName(self):
        return "Factor de escala Landsat (SR + Térmica)"

    def group(self):
        return "Geomaticape"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.png'))


    def groupId(self):
        return "geomaticape"

    def createInstance(self):
        return FactorLandsat()

    # ---------------------------------------------------
    # AYUDA
    # ---------------------------------------------------

    def shortHelpString(self):
        return """
<h3>Factor de escala Landsat Collection 2</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL
<b>Plugin:</b> Geomaticape<br>
<b>Versión:</b> 1.0 <br>
<b>Descripción:</b>
Esta herramienta aplica el factor de escala bandas multiespectrales (A * 0.0000275) - 0.2 y banda termica: (A * 0.00341802) + 149) - 273.15 del satelite LandSat 8 y 9 Colección Level-2 para convertir los valores digitales (DN) a reflectancia de Superficie y genera una imagen multiespectral SR_B1, SR_B2, SR_B3, SR_B4, SR_B5, SR_B6, SR_B7
<b>Proceso:</b>
<ul>
<li> ✔ Aplica el factor de escala bandas multiespectrales y térmicos
<li> ✔ Convierte a unidad de Temperatura superficial °C  
<li> ✔ Salida multibanda con sus nombres respectivos.
<b>Requisito:</b> Landsat 8 y 9
<b>Web:</b> https://www.geomatica.pe/
"""

    # ---------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_FOLDER,
                "Carpeta Landsat Collection 2",
                behavior=QgsProcessingParameterFile.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_MULTISPECTRAL,
                "Imagen multiespectral corregida (B1-B7)"
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_THERMAL,
                "Banda térmica °C (B10)"
            )
        )

    # ---------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        folder = self.parameterAsString(parameters, self.INPUT_FOLDER, context)
        output_multi = self.parameterAsOutputLayer(parameters, self.OUTPUT_MULTISPECTRAL, context)
        output_thermal = self.parameterAsOutputLayer(parameters, self.OUTPUT_THERMAL, context)

        archivos = os.listdir(folder)

        # ---------------------------------------------------
        # CARPETA TEMPORAL PROFESIONAL (NO ENSUCIA USUARIO)
        # ---------------------------------------------------
        temp_dir = tempfile.mkdtemp(prefix="landsat_factor_")
        feedback.pushInfo(f"📂 Temporal interno: {temp_dir}")

        # -----------------------
        # MULTIESPECTRAL B1-B7
        # -----------------------

        bandas_orden = [
            "_SR_B1.TIF",
            "_SR_B2.TIF",
            "_SR_B3.TIF",
            "_SR_B4.TIF",
            "_SR_B5.TIF",
            "_SR_B6.TIF",
            "_SR_B7.TIF"
        ]

        bandas_corregidas = []

        for sufijo in bandas_orden:

            archivo_encontrado = None

            for archivo in archivos:
                if archivo.upper().endswith(sufijo):
                    archivo_encontrado = archivo
                    break

            if not archivo_encontrado:
                raise Exception(f"No se encontró banda {sufijo}")

            ruta = os.path.join(folder, archivo_encontrado)
            salida_temp = os.path.join(temp_dir, archivo_encontrado)

            processing.run(
                "gdal:rastercalculator",
                {
                    'INPUT_A': ruta,
                    'BAND_A': 1,
                    'FORMULA': '(A * 0.0000275) - 0.2',
                    'RTYPE': 5,
                    'NO_DATA': 0,
                    'OUTPUT': salida_temp
                },
                context=context,
                feedback=feedback
            )

            bandas_corregidas.append(salida_temp)

        # -----------------------
        # MERGE MULTIBANDA
        # -----------------------
        feedback.pushInfo("🛰 Generando multibanda corregida...")

        processing.run(
            "gdal:merge",
            {
                'INPUT': bandas_corregidas,
                'SEPARATE': True,
                'OUTPUT': output_multi
            },
            context=context,
            feedback=feedback
        )

        # Renombrar bandas
        dataset = gdal.Open(output_multi, gdal.GA_Update)
        band_names = ["aerosol", "blue", "green", "red", "nir", "swir1", "swir2"]

        for i, name in enumerate(band_names):
            band = dataset.GetRasterBand(i + 1)
            band.SetDescription(name)
            band.SetNoDataValue(0)

        dataset = None

        # -----------------------
        # BANDA TÉRMICA
        # -----------------------

        thermal_file = None

        for archivo in archivos:
            if archivo.upper().endswith("_ST_B10.TIF"):
                thermal_file = archivo
                break

        if not thermal_file:
            raise Exception("No se encontró banda ST_B10")

        ruta_termica = os.path.join(folder, thermal_file)

        feedback.pushInfo("🌡 Calculando temperatura superficial...")

        processing.run(
            "gdal:rastercalculator",
            {
                'INPUT_A': ruta_termica,
                'BAND_A': 1,
                'FORMULA': '((A * 0.00341802) + 149) - 273.15',
                'RTYPE': 5,
                'NO_DATA': -9999,
                'OUTPUT': output_thermal
            },
            context=context,
            feedback=feedback
        )

        dataset = gdal.Open(output_thermal, gdal.GA_Update)
        band = dataset.GetRasterBand(1)
        band.SetDescription("B10_Thermal_Celsius")
        band.SetNoDataValue(-9999)
        dataset = None

        # ---------------------------------------------------
        # LIMPIEZA AUTOMÁTICA PRO
        # ---------------------------------------------------
        feedback.pushInfo("🧹 Eliminando temporales internos...")

        gc.collect()

        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            feedback.pushInfo("✔ Temporales eliminados")
        except:
            feedback.pushInfo("⚠ No se pudo eliminar temporales")

        return {
            self.OUTPUT_MULTISPECTRAL: output_multi,
            self.OUTPUT_THERMAL: output_thermal
        }

    def run(self):
        processing.execAlgorithmDialog(self)
