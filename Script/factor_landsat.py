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
        return "Factor escala Landsat C2 L2 (SR + LST)"

    def group(self):
        return "Conversion"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.png'))


    def groupId(self):
        return "geomaticape_conversion"

    def createInstance(self):
        return FactorLandsat()

    # ---------------------------------------------------
    # AYUDA
    # ---------------------------------------------------

    def shortHelpString(self):
        return """
<h3>Factor de escala Landsat Collection 2 L2</h3>
<b>Autor: </b> GEOMATICA AMBIENTAL
<b>Plugin: </b> Geomaticape
<b>Versión: </b> 1.2 
<b>Aplica factor de escala a:<b>
<ul>
<li> ✔ Landsat 4-5 TM C2 L2
<li> ✔ Landsat 7 ETM+ C2 L2
<li> ✔ Landsat 8-9 OLI/TIRS C2 L2
</ul>
<b>Convierte:<b>
<ul>
<li> ✔ Reflectancia superficial
<li> ✔ Temperatura superficial °C
</ul>
<b>Factor multiespectral:<b>
<ul>
(A * 0.0000275) - 0.2
<ul></b>
<b>Factor térmico:<b>
<ul>
((A * 0.00341802) + 149) - 273.15
<ul>
<b>Proceso:</b>
<ul>
<li> ✔ Aplica el factor de escala bandas multiespectrales y térmicos
<li> ✔ Convierte a unidad de Temperatura superficial °C  
<li> ✔ Salida multibanda con sus nombres respectivos.

<b>Requisito:</b> Landsat Colección 2 Level 2
<b>Web: </b> https://www.geomatica.pe/
"""
    # ---------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_FOLDER,
                "Carpeta Landsat Collection 2 L2",
                behavior=QgsProcessingParameterFile.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_MULTISPECTRAL,
                "Imagen multiespectral corregida"
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_THERMAL,
                "Banda térmica °C"
            )
        )

    # ---------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        folder = self.parameterAsString(parameters, self.INPUT_FOLDER, context)
        output_multi = self.parameterAsOutputLayer(parameters, self.OUTPUT_MULTISPECTRAL, context)
        output_thermal = self.parameterAsOutputLayer(parameters, self.OUTPUT_THERMAL, context)

        archivos = os.listdir(folder)

        # ---------------------------------------------------
        # DETECTAR SATÉLITE POR PREFIJO USGS
        # ---------------------------------------------------
        prefijo = None

        for archivo in archivos:
            nombre = archivo.upper()

            if nombre.startswith("LC08"):
                prefijo = "LC08"
                break
            elif nombre.startswith("LC09"):
                prefijo = "LC09"
                break
            elif nombre.startswith("LE07"):
                prefijo = "LE07"
                break
            elif nombre.startswith("LT05"):
                prefijo = "LT05"
                break
            elif nombre.startswith("LT04"):
                prefijo = "LT04"
                break

        if not prefijo:
            raise Exception("No se reconoce el satélite Landsat")

        # ---------------------------------------------------
        # CONFIGURACIÓN SEGÚN SATÉLITE
        # ---------------------------------------------------
        if prefijo in ["LC08", "LC09"]:
            feedback.pushInfo(f"🛰 Detectado: Landsat 8/9 ({prefijo})")

            bandas_orden = [
                "_SR_B1.TIF","_SR_B2.TIF","_SR_B3.TIF",
                "_SR_B4.TIF","_SR_B5.TIF","_SR_B6.TIF","_SR_B7.TIF"
            ]
            nombres = ["aerosol","blue","green","red","nir","swir1","swir2"]
            thermal_suffix = "_ST_B10.TIF"
            thermal_name = "B10_Thermal_Celsius"

        elif prefijo == "LE07":
            feedback.pushInfo("🛰 Detectado: Landsat 7 ETM+")

            bandas_orden = [
                "_SR_B1.TIF","_SR_B2.TIF","_SR_B3.TIF",
                "_SR_B4.TIF","_SR_B5.TIF","_SR_B7.TIF"
            ]
            nombres = ["blue","green","red","nir","swir1","swir2"]
            thermal_suffix = "_ST_B6.TIF"
            thermal_name = "B6_Thermal_Celsius"

        elif prefijo in ["LT05", "LT04"]:
            feedback.pushInfo(f"🛰 Detectado: Landsat 4/5 TM ({prefijo})")

            bandas_orden = [
                "_SR_B1.TIF","_SR_B2.TIF","_SR_B3.TIF",
                "_SR_B4.TIF","_SR_B5.TIF","_SR_B7.TIF"
            ]
            nombres = ["blue","green","red","nir","swir1","swir2"]
            thermal_suffix = "_ST_B6.TIF"
            thermal_name = "B6_Thermal_Celsius"

        # ---------------------------------------------------
        # TEMPORAL PRO
        # ---------------------------------------------------
        temp_dir = tempfile.mkdtemp(prefix="landsat_factor_")
        feedback.pushInfo(f"📂 Carpeta temporal: {temp_dir}")

        # ---------------------------------------------------
        # MULTIESPECTRAL
        # ---------------------------------------------------
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

        # ---------------------------------------------------
        # MERGE MULTIBANDA
        # ---------------------------------------------------
        feedback.pushInfo("🛰 Generando multibanda...")

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

        dataset = gdal.Open(output_multi, gdal.GA_Update)

        for i, name in enumerate(nombres):
            band = dataset.GetRasterBand(i + 1)
            band.SetDescription(name)
            band.SetNoDataValue(0)

        dataset = None

        # ---------------------------------------------------
        # TÉRMICA
        # ---------------------------------------------------
        thermal_file = None

        for archivo in archivos:
            if archivo.upper().endswith(thermal_suffix):
                thermal_file = archivo
                break

        if not thermal_file:
            raise Exception("No se encontró banda térmica")

        ruta_termica = os.path.join(folder, thermal_file)

        feedback.pushInfo("🌡 Calculando temperatura °C...")

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
        band.SetDescription(thermal_name)
        band.SetNoDataValue(-9999)
        dataset = None

        # ---------------------------------------------------
        # LIMPIEZA
        # ---------------------------------------------------
        feedback.pushInfo("🧹 Eliminando temporales...")
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