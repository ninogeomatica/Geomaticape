import os
import gc
import numpy as np
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterFileDestination,
    QgsProcessingException
)
from qgis import processing
from osgeo import gdal


class ACPSatelite(QgsProcessingAlgorithm):
    """
    Análisis de Componentes Principales (ACP / PCA) sobre una imagen
    multiespectral de cualquier satélite (Landsat, Sentinel-2, CBERS,
    PlanetScope, RapidEye, ASTER, etc.).

    Entrada:
        - Imagen multiespectral (n bandas, cualquier resolución espacial)
    Salidas:
        - Raster ACP de 3 bandas (PCA1, PCA2, PCA3), ordenadas por mayor
          varianza explicada (PCA1 = mayor componente).
        - Gráfico de barras PNG con el % de varianza explicada por cada
          componente.
        - CSV con la matriz de correlación entre las bandas originales.
    """

    INPUT_RASTER       = "INPUT_RASTER"
    OUTPUT_ACP         = "OUTPUT_ACP"
    OUTPUT_BARRA       = "OUTPUT_BARRA"
    OUTPUT_CORR_CSV    = "OUTPUT_CORR_CSV"
    OUTPUT_CORR_PNG    = "OUTPUT_CORR_PNG"

    # -------------------------------------------------------
    # IDENTIFICACIÓN
    # -------------------------------------------------------

    def name(self):
        return "acp_satelite"

    def displayName(self):
        return "ACP Multiespectral (cualquier satélite)"

    def group(self):
        return "Procesamiento"

    def groupId(self):
        return "geomaticape_procesamiento"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), "..", "Icons", "acp.png"))

    def createInstance(self):
        return ACPSatelite()

    # -------------------------------------------------------
    # AYUDA
    # -------------------------------------------------------

    def shortHelpString(self):
        return """
<h3>ACP — Análisis de Componentes Principales Multiespectral</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Versión:</b> 1.4<br><br>

<b>Descripción:</b><br>
Aplica el Análisis de Componentes Principales (ACP / PCA) a una imagen
multiespectral de cualquier satélite. Detecta automáticamente la
resolución espacial y el número de bandas de la imagen de entrada.
La salida es un raster de 3 bandas con los componentes más informativos
(PCA1, PCA2, PCA3), ordenados de mayor a menor varianza explicada.

<b>Satélites compatibles (ejemplos):</b>
<ul>
<li>✔ Landsat 4/5 TM — 6 bandas — 30 m</li>
<li>✔ Landsat 7 ETM+ — 6 bandas — 30 m</li>
<li>✔ Landsat 8/9 OLI — 6 bandas — 30 m</li>
<li>✔ Sentinel-2 MSI — 10 bandas — 10/20 m</li>
<li>✔ CBERS-04A WPM — 4 bandas — 8 m / 2 m</li>
<li>✔ PlanetScope, RapidEye, ASTER, MODIS, etc.</li>
</ul>

<b>Proceso:</b>
<ul>
<li>✔ Lee todas las bandas de la imagen de entrada</li>
<li>✔ Calcula la matriz de correlación entre bandas (CSV + PNG)</li>
<li>✔ Estandariza los datos (StandardScaler)</li>
<li>✔ Ejecuta PCA (sklearn.decomposition.PCA)</li>
<li>✔ Genera gráfico de barras con el % de varianza explicada</li>
<li>✔ Exporta los 3 componentes principales como raster GeoTIFF Float32</li>
<li>✔ Conserva CRS y geotransform de la imagen original</li>
<li>✔ PCA1 → mayor varianza explicada · PCA3 → tercera</li>
</ul>

<b>Bandas de salida:</b><br>
PCA1 · PCA2 · PCA3

<b>Requisito:</b> Imagen raster multiespectral (≥ 3 bandas).<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    # -------------------------------------------------------
    # PARÁMETROS
    # -------------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                "Imagen multiespectral de entrada (n bandas)"
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_ACP,
                "Imagen ACP — 3 bandas (PCA1, PCA2, PCA3)"
            )
        )

        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_BARRA,
                "Gráfico de barras — % varianza explicada (PNG)",
                fileFilter="PNG (*.png)"
            )
        )

        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_CORR_CSV,
                "Matriz de correlación entre bandas (CSV)",
                fileFilter="CSV (*.csv)",
                optional=True,
                createByDefault=False
            )
        )

        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_CORR_PNG,
                "Gráfico matriz de correlación (PNG)",
                fileFilter="PNG (*.png)",
                optional=True,
                createByDefault=False
            )
        )

    # -------------------------------------------------------
    # PROCESO PRINCIPAL
    # -------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        # --- Dependencias externas (sklearn, matplotlib, pandas) ---
        try:
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
            from sklearn.decomposition import PCA
        except ImportError:
            raise QgsProcessingException(
                "❌ Falta la librería 'scikit-learn'. Instálala con:\n"
                "   python -m pip install scikit-learn"
            )

        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            raise QgsProcessingException(
                "❌ Falta la librería 'matplotlib'. Instálala con:\n"
                "   python -m pip install matplotlib"
            )

        try:
            import pandas as pd
        except ImportError:
            raise QgsProcessingException(
                "❌ Falta la librería 'pandas'. Instálala con:\n"
                "   python -m pip install pandas"
            )

        # ---------------------------------------------------
        # 1. PARÁMETROS Y APERTURA DE LA IMAGEN
        # ---------------------------------------------------
        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context)
        if raster_layer is None:
            raise QgsProcessingException("❌ No se pudo leer el raster de entrada.")

        input_path  = raster_layer.source()
        output_acp  = self.parameterAsOutputLayer(parameters, self.OUTPUT_ACP, context)
        output_bar  = self.parameterAsFileOutput(parameters, self.OUTPUT_BARRA, context)
        output_corr = self.parameterAsFileOutput(parameters, self.OUTPUT_CORR_CSV, context)
        output_cpng = self.parameterAsFileOutput(parameters, self.OUTPUT_CORR_PNG, context)

        feedback.pushInfo(f"📥 Raster de entrada: {os.path.basename(input_path)}")

        ds_in = gdal.Open(input_path, gdal.GA_ReadOnly)
        if ds_in is None:
            raise QgsProcessingException("❌ GDAL no pudo abrir la imagen.")

        cols   = ds_in.RasterXSize
        rows   = ds_in.RasterYSize
        nbands = ds_in.RasterCount
        gt     = ds_in.GetGeoTransform()
        proj   = ds_in.GetProjection()
        res_x  = abs(gt[1])
        res_y  = abs(gt[5])

        feedback.pushInfo(f"🛰  Dimensiones    : {cols} x {rows} px")
        feedback.pushInfo(f"🛰  N° de bandas   : {nbands}")
        feedback.pushInfo(f"🛰  Resolución esp.: {res_x:.2f} x {res_y:.2f} m")

        if nbands < 3:
            raise QgsProcessingException(
                f"❌ La imagen tiene solo {nbands} banda(s). "
                "Se requieren mínimo 3 bandas para extraer PCA1, PCA2 y PCA3."
            )

        # ---------------------------------------------------
        # 2. LEER BANDAS Y APILAR (rows*cols, n_bandas)
        # ---------------------------------------------------
        feedback.pushInfo("📊 Leyendo y apilando bandas...")

        # Detectar nodata para máscara
        nodata_vals = []
        band_names_in = []
        for i in range(1, nbands + 1):
            band = ds_in.GetRasterBand(i)
            nd = band.GetNoDataValue()
            nodata_vals.append(nd)
            desc = band.GetDescription()
            if not desc:
                desc = f"B{i}"
            band_names_in.append(desc)

        # Carga el cubo (rows, cols, bands)
        cube = np.zeros((rows, cols, nbands), dtype=np.float64)
        for i in range(nbands):
            arr = ds_in.GetRasterBand(i + 1).ReadAsArray().astype(np.float64)
            if nodata_vals[i] is not None:
                arr[arr == nodata_vals[i]] = np.nan
            cube[:, :, i] = arr
            feedback.setProgress(int(5 + (i + 1) / nbands * 15))

        ds_in = None

        # Reformar a matriz (n_pixeles, n_bandas)
        datos_full = cube.reshape((rows * cols, nbands))

        # Máscara de filas válidas (sin NaN en ninguna banda)
        mask_validos = ~np.isnan(datos_full).any(axis=1)
        datos_validos = datos_full[mask_validos]

        if datos_validos.shape[0] < 100:
            raise QgsProcessingException(
                "❌ Muy pocos píxeles válidos para calcular el PCA."
            )

        feedback.pushInfo(
            f"   Píxeles válidos: {datos_validos.shape[0]:,} / {datos_full.shape[0]:,}"
        )

        # ---------------------------------------------------
        # 3. MATRIZ DE CORRELACIÓN (CSV + PNG opcional)
        # ---------------------------------------------------
        feedback.pushInfo("📐 Calculando matriz de correlación...")

        df  = pd.DataFrame(datos_validos, columns=band_names_in)
        corr = df.corr()

        if output_corr:
            try:
                corr.to_csv(output_corr, index=True, encoding="utf-8")
                feedback.pushInfo(f"   ✔ CSV de correlación: {output_corr}")
            except Exception as e:
                feedback.pushInfo(f"   ⚠ No se pudo guardar CSV: {e}")

        if output_cpng:
            try:
                fig, ax = plt.subplots(figsize=(8, 8))
                cax = ax.matshow(corr.values, cmap="RdYlGn_r", vmin=-1, vmax=1)
                ax.set_xticks(range(nbands))
                ax.set_yticks(range(nbands))
                ax.set_xticklabels(band_names_in, fontsize=11, rotation=45, ha="left")
                ax.set_yticklabels(band_names_in, fontsize=11)

                for i in range(nbands):
                    for j in range(nbands):
                        ax.text(
                            j, i, f"{corr.iloc[i, j]:.2f}",
                            ha="center", va="center",
                            fontsize=9, color="black"
                        )

                ax.set_title("Matriz de correlación entre bandas", fontsize=14, pad=15)
                fig.colorbar(cax, fraction=0.046, pad=0.04)
                fig.tight_layout()
                fig.savefig(output_cpng, dpi=200, bbox_inches="tight")
                plt.close(fig)
                feedback.pushInfo(f"   ✔ PNG de correlación: {output_cpng}")
            except Exception as e:
                feedback.pushInfo(f"   ⚠ No se pudo generar PNG correlación: {e}")

        # ---------------------------------------------------
        # 4. ENTRENAMIENTO DEL MODELO PCA
        # ---------------------------------------------------
        feedback.pushInfo("🧠 Entrenando modelo PCA (StandardScaler + PCA)...")

        pipeline = make_pipeline(StandardScaler(), PCA())
        pipeline.fit(datos_validos)
        modelo_pca = pipeline.named_steps["pca"]

        var_ratio = modelo_pca.explained_variance_ratio_
        var_pct   = var_ratio * 100.0

        feedback.pushInfo("📈 % varianza explicada por componente:")
        for i, v in enumerate(var_pct, start=1):
            feedback.pushInfo(f"   PCA{i}: {v:.4f} %")

        var_total_3 = var_pct[:3].sum()
        feedback.pushInfo(f"   ➤ PCA1+PCA2+PCA3 = {var_total_3:.2f} % varianza")

        feedback.setProgress(50)

        # ---------------------------------------------------
        # 5. GRÁFICO DE BARRAS — % VARIANZA EXPLICADA
        # ---------------------------------------------------
        feedback.pushInfo("📊 Generando gráfico de barras de varianza...")

        fig, ax = plt.subplots(figsize=(9, 6))

        x_pos = np.arange(nbands) + 1
        # Coloreamos las 3 primeras barras distinto (las que se exportan)
        colors = ["#2ECC71" if i < 3 else "#BDC3C7" for i in range(nbands)]

        bars = ax.bar(x_pos, var_pct, color=colors, edgecolor="black", linewidth=0.7)

        for x, y in zip(x_pos, var_pct):
            ax.annotate(
                f"{y:.2f}%",
                (x, y),
                textcoords="offset points",
                xytext=(0, 5),
                ha="center",
                fontsize=10
            )

        ax.set_xticks(x_pos)
        ax.set_xticklabels([f"PCA{i}" for i in x_pos], fontsize=11)
        ax.set_title(
            f"Porcentaje de varianza explicada por componente\n"
            f"(PCA1+PCA2+PCA3 = {var_total_3:.2f} %)",
            fontsize=13
        )
        ax.set_xlabel("Componentes principales", fontsize=11)
        ax.set_ylabel("% Varianza explicada", fontsize=11)
        y_max = max(var_pct.max() * 1.15, 10)
        ax.set_ylim(0, y_max)
        ax.grid(axis="y", linestyle="--", alpha=0.4)

        fig.tight_layout()
        fig.savefig(output_bar, dpi=200, bbox_inches="tight")
        plt.close(fig)

        feedback.pushInfo(f"   ✔ PNG varianza: {output_bar}")
        feedback.setProgress(65)

        # ---------------------------------------------------
        # 6. TRANSFORMAR Y RECONSTRUIR LAS COMPONENTES
        # ---------------------------------------------------
        feedback.pushInfo("🔄 Proyectando datos a componentes principales...")

        # Aplicar el scaler entrenado y transformar TODA la imagen.
        # Para los píxeles con NaN ponemos 0 (luego enmascaramos).
        scaler = pipeline.named_steps["standardscaler"]

        datos_full_safe = np.where(np.isnan(datos_full), 0.0, datos_full)
        datos_std       = scaler.transform(datos_full_safe)
        x_pca           = modelo_pca.transform(datos_std)   # (rows*cols, nbands)

        # Solo nos quedamos con las 3 primeras componentes
        pca_3 = x_pca[:, :3]

        # Re-aplicamos NaN a los píxeles inválidos
        pca_3[~mask_validos, :] = np.nan

        # Reformar a (rows, cols, 3)
        pca_img = pca_3.reshape((rows, cols, 3)).astype(np.float32)

        feedback.setProgress(85)

        # ---------------------------------------------------
        # 7. EXPORTAR RASTER ACP (3 BANDAS)
        # ---------------------------------------------------
        feedback.pushInfo("💾 Exportando raster ACP (3 bandas)...")

        driver = gdal.GetDriverByName("GTiff")
        ds_out = driver.Create(
            output_acp,
            cols,
            rows,
            3,
            gdal.GDT_Float32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out.SetGeoTransform(gt)
        ds_out.SetProjection(proj)

        nodata_out = -9999.0
        band_labels = [
            f"PCA1 ({var_pct[0]:.2f}%)",
            f"PCA2 ({var_pct[1]:.2f}%)",
            f"PCA3 ({var_pct[2]:.2f}%)",
        ]

        for i in range(3):
            arr = pca_img[:, :, i]
            arr = np.where(np.isnan(arr), nodata_out, arr).astype(np.float32)
            band = ds_out.GetRasterBand(i + 1)
            band.WriteArray(arr)
            band.SetDescription(band_labels[i])
            band.SetNoDataValue(nodata_out)

        ds_out.FlushCache()
        ds_out = None

        feedback.pushInfo(f"   ✔ ACP exportado: {output_acp}")
        feedback.setProgress(100)

        # ---------------------------------------------------
        # 8. LIMPIEZA
        # ---------------------------------------------------
        del cube, datos_full, datos_validos, datos_full_safe, datos_std, x_pca, pca_3, pca_img
        gc.collect()

        feedback.pushInfo("✅ ACP MULTIESPECTRAL COMPLETO")

        results = {
            self.OUTPUT_ACP: output_acp,
            self.OUTPUT_BARRA: output_bar,
        }
        if output_corr:
            results[self.OUTPUT_CORR_CSV] = output_corr
        if output_cpng:
            results[self.OUTPUT_CORR_PNG] = output_cpng

        return results

    def run(self):
        processing.execAlgorithmDialog(self)
