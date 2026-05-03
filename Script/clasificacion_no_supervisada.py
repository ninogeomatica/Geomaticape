import os
import gc
import numpy as np
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException
)
from qgis import processing
from osgeo import gdal


# Catalogo de metodos de clasificacion no supervisada
METHODS = [
    "K-Means",
    "MiniBatch K-Means (rapido en imagenes grandes)",
    "Gaussian Mixture Model (GMM)",
    "ISODATA simplificado (K-Means iterativo)",
    "Birch (clustering jerarquico balanceado)",
]
METHOD_KEYS = ["kmeans", "minibatch", "gmm", "isodata", "birch"]


class ClasificacionNoSupervisada(QgsProcessingAlgorithm):
    """
    Clasificacion no supervisada de imagenes multiespectrales.
    El usuario elige el numero de clases y el metodo.
    Salida: raster de 1 banda (Int16) con etiquetas 1..K (0 = nodata).
    """

    INPUT_RASTER  = "INPUT_RASTER"
    METHOD        = "METHOD"
    N_CLASSES     = "N_CLASSES"
    SAMPLE_SIZE   = "SAMPLE_SIZE"
    STANDARDIZE   = "STANDARDIZE"
    RANDOM_SEED   = "RANDOM_SEED"
    OUTPUT_RASTER = "OUTPUT_RASTER"

    # -------------------------------------------------------
    # IDENTIFICACION
    # -------------------------------------------------------

    def name(self):
        return "clasificacion_no_supervisada"

    def displayName(self):
        return "Clasificacion no supervisada (K-Means / GMM / ISODATA / Birch)"

    def group(self):
        return "Procesamiento"

    def groupId(self):
        return "geomaticape_procesamiento"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), "..", "Icons", "clasificacion.png"))

    def createInstance(self):
        return ClasificacionNoSupervisada()

    # -------------------------------------------------------
    # AYUDA
    # -------------------------------------------------------

    def shortHelpString(self):
        return """
<h3>Clasificacion no supervisada multiespectral</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.6<br><br>

<b>Descripcion:</b><br>
Aplica clasificacion no supervisada (clustering) sobre una imagen
multiespectral de cualquier satelite. Detecta automaticamente el numero
de bandas y la resolucion espacial de la imagen de entrada. La salida
es un raster de 1 banda (Int16) con etiquetas 1..K, donde K es el numero
de clases que el usuario haya indicado.

<b>Metodos disponibles:</b>
<ul>
<li><b>K-Means</b>: agrupamiento clasico por minimizacion de la varianza
intracluster. Rapido y robusto, ideal para coberturas espectralmente
diferenciadas (agua, suelo, vegetacion).</li>
<li><b>MiniBatch K-Means</b>: variante optimizada de K-Means que entrena
con sub-muestras. Recomendado para imagenes grandes (Sentinel-2 a 10m,
mosaicos, escenas completas Landsat).</li>
<li><b>Gaussian Mixture Model (GMM)</b>: clustering probabilistico que
admite agrupamientos elipticos y de tamanos distintos. Mas flexible que
K-Means pero mas lento.</li>
<li><b>ISODATA simplificado</b>: K-Means con varios reinicios
(n_init=10) y mas iteraciones; util cuando el usuario quiere un
resultado mas estable que un K-Means estandar.</li>
<li><b>Birch</b>: clustering jerarquico balanceado, util cuando se busca
una particion incremental de la imagen.</li>
</ul>

<b>Parametros:</b>
<ul>
<li><b>Numero de clases (K)</b>: cantidad de grupos a generar (2..30).</li>
<li><b>Tamano de muestra de entrenamiento</b>: numero de pixeles
aleatorios usados para entrenar el modelo (acelera mucho el ajuste sin
perder calidad). Default: 100,000 pixeles.</li>
<li><b>Estandarizar bandas</b>: aplica StandardScaler antes del
clustering. Recomendado para combinar bandas con escalas distintas.</li>
<li><b>Semilla aleatoria</b>: reproducibilidad del resultado.</li>
</ul>

<b>Satelites compatibles:</b>
<ul>
<li>Landsat 4/5 TM, 7 ETM+, 8/9 OLI</li>
<li>Sentinel-2 MSI L1C / L2A</li>
<li>CBERS-04A WPM</li>
<li>PlanetScope, RapidEye, ASTER, MODIS, drones, etc.</li>
</ul>

<b>Salida:</b> raster GeoTIFF Int16 (1 banda, valores 1..K, nodata = 0).
<b>Web:</b> https://www.geomatica.pe/
"""

    # -------------------------------------------------------
    # PARAMETROS
    # -------------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                "Imagen multiespectral de entrada"
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.METHOD,
                "Metodo de clasificacion no supervisada",
                options=METHODS,
                defaultValue=0,
                allowMultiple=False
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.N_CLASSES,
                "Numero de clases (K)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=5,
                minValue=2,
                maxValue=30
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.SAMPLE_SIZE,
                "Tamano de muestra de entrenamiento (pixeles)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=100000,
                minValue=1000,
                maxValue=10000000
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.STANDARDIZE,
                "Estandarizar bandas (StandardScaler) - recomendado",
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.RANDOM_SEED,
                "Semilla aleatoria (reproducibilidad)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=42,
                minValue=0,
                maxValue=999999
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_RASTER,
                "Raster de clases (Int16, 1 banda)"
            )
        )

    # -------------------------------------------------------
    # PROCESO PRINCIPAL
    # -------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        # Verificar dependencias
        try:
            from sklearn.cluster import KMeans, MiniBatchKMeans, Birch
            from sklearn.mixture import GaussianMixture
            from sklearn.preprocessing import StandardScaler
        except ImportError:
            raise QgsProcessingException(
                "Falta la libreria 'scikit-learn'. Instalala con:\n"
                "   python -m pip install scikit-learn"
            )

        # Parametros
        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster_layer is None:
            raise QgsProcessingException("No se pudo leer el raster de entrada.")
        input_path = raster_layer.source()

        method_idx  = self.parameterAsEnum(parameters, self.METHOD, context)
        method_key  = METHOD_KEYS[method_idx]
        method_name = METHODS[method_idx]
        n_classes   = self.parameterAsInt(parameters, self.N_CLASSES, context)
        sample_size = self.parameterAsInt(parameters, self.SAMPLE_SIZE, context)
        standardize = self.parameterAsBool(parameters, self.STANDARDIZE, context)
        seed        = self.parameterAsInt(parameters, self.RANDOM_SEED, context)
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT_RASTER, context)

        feedback.pushInfo("====================================================")
        feedback.pushInfo(f"Metodo      : {method_name}")
        feedback.pushInfo(f"K (clases)  : {n_classes}")
        feedback.pushInfo(f"Muestra     : {sample_size:,} pixeles")
        feedback.pushInfo(f"Estandarizar: {standardize}")
        feedback.pushInfo(f"Semilla     : {seed}")
        feedback.pushInfo("====================================================")

        # ---------------------------------------------------
        # 1. ABRIR Y LEER LA IMAGEN
        # ---------------------------------------------------
        ds_in = gdal.Open(input_path, gdal.GA_ReadOnly)
        if ds_in is None:
            raise QgsProcessingException("GDAL no pudo abrir la imagen.")

        cols   = ds_in.RasterXSize
        rows   = ds_in.RasterYSize
        nbands = ds_in.RasterCount
        gt     = ds_in.GetGeoTransform()
        proj   = ds_in.GetProjection()

        feedback.pushInfo(f"Imagen     : {os.path.basename(input_path)}")
        feedback.pushInfo(f"Dimensiones: {cols} x {rows} px, {nbands} bandas")
        feedback.pushInfo(f"Resolucion : {abs(gt[1]):.2f} x {abs(gt[5]):.2f} m")
        feedback.setProgress(5)

        # ---------------------------------------------------
        # 2. APILAR BANDAS (rows*cols, n_bandas)
        # ---------------------------------------------------
        feedback.pushInfo("Apilando bandas...")
        cube = np.zeros((rows, cols, nbands), dtype=np.float32)
        nodata_vals = []
        for i in range(nbands):
            band = ds_in.GetRasterBand(i + 1)
            arr = band.ReadAsArray().astype(np.float32)
            nd = band.GetNoDataValue()
            nodata_vals.append(nd)
            if nd is not None:
                arr[arr == nd] = np.nan
            cube[:, :, i] = arr
            feedback.setProgress(5 + int((i + 1) / nbands * 15))

        ds_in = None

        datos_full = cube.reshape((rows * cols, nbands))
        mask_validos = ~np.isnan(datos_full).any(axis=1)
        datos_validos = datos_full[mask_validos]

        if datos_validos.shape[0] < n_classes * 10:
            raise QgsProcessingException(
                f"Pocos pixeles validos ({datos_validos.shape[0]}). "
                f"Se necesitan al menos {n_classes*10} para K={n_classes}."
            )

        feedback.pushInfo(
            f"Pixeles validos: {datos_validos.shape[0]:,} / {datos_full.shape[0]:,}"
        )
        feedback.setProgress(25)

        # ---------------------------------------------------
        # 3. ESTANDARIZAR (opcional)
        # ---------------------------------------------------
        scaler = None
        if standardize:
            feedback.pushInfo("Estandarizando bandas (StandardScaler)...")
            scaler = StandardScaler()
            datos_validos_std = scaler.fit_transform(datos_validos)
        else:
            datos_validos_std = datos_validos

        # ---------------------------------------------------
        # 4. SUBMUESTREO PARA EL ENTRENAMIENTO
        # ---------------------------------------------------
        n_total = datos_validos_std.shape[0]
        rng = np.random.default_rng(seed)

        if n_total > sample_size:
            feedback.pushInfo(
                f"Tomando muestra aleatoria de {sample_size:,} pixeles "
                f"de {n_total:,} para entrenamiento..."
            )
            idx = rng.choice(n_total, size=sample_size, replace=False)
            train = datos_validos_std[idx]
        else:
            train = datos_validos_std
            feedback.pushInfo(
                f"La imagen tiene {n_total:,} pixeles validos: "
                "se usa la imagen completa para entrenar."
            )

        feedback.setProgress(35)

        # ---------------------------------------------------
        # 5. ENTRENAR EL MODELO
        # ---------------------------------------------------
        feedback.pushInfo(f"Entrenando modelo ({method_name})...")

        if method_key == "kmeans":
            model = KMeans(
                n_clusters=n_classes,
                random_state=seed,
                n_init=10,
                max_iter=300
            )
            model.fit(train)

        elif method_key == "minibatch":
            model = MiniBatchKMeans(
                n_clusters=n_classes,
                random_state=seed,
                batch_size=2048,
                n_init=5,
                max_iter=300
            )
            model.fit(train)

        elif method_key == "gmm":
            model = GaussianMixture(
                n_components=n_classes,
                random_state=seed,
                covariance_type="full",
                max_iter=200,
                n_init=1
            )
            model.fit(train)

        elif method_key == "isodata":
            # ISODATA simplificado: K-Means con muchos reinicios
            model = KMeans(
                n_clusters=n_classes,
                random_state=seed,
                n_init=20,
                max_iter=500,
                tol=1e-5
            )
            model.fit(train)

        elif method_key == "birch":
            # Threshold heuristico segun la dispersion de los datos
            model = Birch(
                n_clusters=n_classes,
                threshold=0.5,
                branching_factor=50
            )
            model.fit(train)

        else:
            raise QgsProcessingException(f"Metodo no soportado: {method_key}")

        feedback.setProgress(65)

        # ---------------------------------------------------
        # 6. PREDECIR PARA TODA LA IMAGEN (por bloques)
        # ---------------------------------------------------
        feedback.pushInfo("Clasificando todos los pixeles validos...")

        # Procesar en bloques para no consumir RAM
        chunk = 500_000
        labels = np.empty(n_total, dtype=np.int32)
        for start in range(0, n_total, chunk):
            end = min(start + chunk, n_total)
            block = datos_validos_std[start:end]
            labels[start:end] = model.predict(block)
            feedback.setProgress(65 + int((end / n_total) * 25))

        # Etiquetas 1..K (0 reservado para nodata)
        labels = labels.astype(np.int16) + 1

        # Reasignar al cubo completo
        result_full = np.zeros(rows * cols, dtype=np.int16)  # 0 = nodata
        result_full[mask_validos] = labels
        result_img = result_full.reshape((rows, cols))

        feedback.setProgress(92)

        # ---------------------------------------------------
        # 7. ESTADISTICAS POR CLASE
        # ---------------------------------------------------
        feedback.pushInfo("Distribucion de clases:")
        unique, counts = np.unique(labels, return_counts=True)
        total_valid = labels.size
        for u, c in zip(unique, counts):
            pct = 100.0 * c / total_valid
            feedback.pushInfo(f"  Clase {int(u):>2d}: {int(c):>10,} px  ({pct:6.2f} %)")

        # ---------------------------------------------------
        # 8. EXPORTAR RASTER
        # ---------------------------------------------------
        feedback.pushInfo(f"Exportando raster a: {output_path}")

        driver = gdal.GetDriverByName("GTiff")
        ds_out = driver.Create(
            output_path, cols, rows, 1, gdal.GDT_Int16,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out.SetGeoTransform(gt)
        ds_out.SetProjection(proj)

        out_band = ds_out.GetRasterBand(1)
        out_band.WriteArray(result_img)
        out_band.SetDescription(f"Clases_{method_key}_K{n_classes}")
        out_band.SetNoDataValue(0)

        ds_out.FlushCache()
        ds_out = None

        feedback.setProgress(100)

        # Limpieza
        del cube, datos_full, datos_validos, datos_validos_std, train, labels, result_full, result_img
        gc.collect()

        feedback.pushInfo("CLASIFICACION COMPLETADA")

        return {self.OUTPUT_RASTER: output_path}

    def run(self):
        processing.execAlgorithmDialog(self)
