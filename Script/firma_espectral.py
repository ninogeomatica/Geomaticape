# -*- coding: utf-8 -*-
"""
firma_espectral.py  ·  GeomaticaPE v1.2.6
==========================================
Firma espectral de coberturas para:
  · Landsat 5 TM  (B1-B3, B4, B5, B7)
  · Landsat 7 ETM (B1-B3, B4, B5, B7)
  · Landsat 8 OLI (B2-B7)
  · Landsat 9 OLI (B2-B7)  ← mismas longitudes que L8
  · Sentinel-2 MSI (B1-B8A, B11, B12)
  · ASTER L1T RS  (B1-B9)

Flujo:
  1. Lee la imagen multiespectral (reflectancia superficie)
  2. Lee la capa de puntos con campo "Clase" (o configurable)
  3. Extrae el valor de cada banda por punto
  4. Agrupa por clase: calcula media, mínimo, máximo
  5. Exporta Excel (tabla puntos + tabla resumen por clase)
  6. Genera gráfico PNG de firma espectral (mean ± min/max como banda de relleno)
"""

import os
import math

import numpy as np

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterBoolean,
    QgsProcessing,
)


# ── Configuración de sensores ─────────────────────────────────────────────────

SENSORES = {
    'Landsat 5 TM': {
        'bandas':    ['blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'longitud':  [0.490,   0.560,   0.660,   0.830,   1.650,   2.220],
        'factor':    1.0,
        'titulo':    'Firma Espectral — Landsat 5 TM',
    },
    'Landsat 7 ETM': {
        'bandas':    ['blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'longitud':  [0.485,   0.560,   0.660,   0.830,   1.650,   2.220],
        'factor':    1.0,
        'titulo':    'Firma Espectral — Landsat 7 ETM+',
    },
    'Landsat 8 OLI': {
        'bandas':    ['aerosol','blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'longitud':  [0.440, 0.480,   0.560,   0.660,   0.865,   1.610,   2.200],
        'factor':    1.0,
        'titulo':    'Firma Espectral — Landsat 8 OLI',
    },
    'Landsat 9 OLI': {
        'bandas':    ['aerosol','blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'longitud':  [0.440, 0.480,   0.560,   0.660,   0.865,   1.610,   2.200],
        'factor':    1.0,
        'titulo':    'Firma Espectral — Landsat 9 OLI-2',
    },
    'Sentinel-2 MSI': {
        'bandas':    ['aerosol','blue', 'green', 'red',  'red_edge1',
                      'red_edge2', 'red_edge3', 'nir',  'swir1', 'swir2'],
        'longitud':  [0.4427,  0.4924,  0.5598,   0.6646,   0.7041,
                      0.7405,  0.7828,  0.8328,   1.6137,   2.2024],
        'factor':    1.0,
        'titulo':    'Firma Espectral — Sentinel-2 MSI',
    },
    'ASTER L1T': {
        'bandas':    ['green', 'red', 'red_edge_3N', 'swir1', 'swir2',
                      'swir3', 'swir4', 'swir5', 'swir6'],
        'longitud':  [0.560,   0.660,   0.820,   1.650,   2.165,
                      2.205,   2.260,   2.330,   2.395],
        'factor':    1.0,
        'titulo':    'Firma Espectral — ASTER L1T',
    },
}

# Paleta de colores para clases (se repite cíclicamente)
COLORES = [
    '#2ecc71',  # verde
    '#3498db',  # azul
    '#e74c3c',  # rojo
    '#9b59b6',  # morado
    '#f39c12',  # naranja
    '#1abc9c',  # turquesa
    '#e67e22',  # naranja oscuro
    '#34495e',  # gris oscuro
    '#c0392b',  # rojo oscuro
    '#16a085',  # verde mar
]


# ──────────────────────────────────────────────────────────────────────────────

class FirmaEspectral(QgsProcessingAlgorithm):

    RASTER   = 'RASTER'
    PUNTOS   = 'PUNTOS'
    CAMPO    = 'CAMPO'
    SENSOR   = 'SENSOR'
    EXCEL    = 'EXCEL'
    GRAFICO  = 'GRAFICO'

    def createInstance(self):
        return FirmaEspectral()

    def name(self):
        return 'firma_espectral'

    def displayName(self):
        return 'Firma espectral (Landsat 5/7/8/9 · Sentinel-2 · ASTER)'

    def group(self):
        return 'Procesamiento'

    def groupId(self):
        return 'procesamiento'

    def tags(self):
        return ['firma', 'espectral', 'landsat', 'sentinel', 'aster',
                'reflectancia', 'cobertura', 'clase', 'muestreo',
                'longitud de onda', 'grafico', 'excel']

    def icon(self):
        return QIcon(os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'Icons', 'indices.png'
        ))

    def shortHelpString(self):
        return (
            '<b>Firma Espectral por Cobertura</b><br><br>'
            'Extrae la reflectancia superficial en puntos de cobertura y genera '
            'curvas de firma espectral agrupadas por clase.<br><br>'
            '<b>Entradas:</b><br>'
            '· <b>Imagen multiespectral</b>: reflectancia superficie (RS)<br>'
            '· <b>Capa de puntos</b>: con campo de clase/cobertura (ej. "Bosque", "Agua")<br>'
            '· <b>Sensor</b>: define las longitudes de onda y factor de escala<br><br>'
            '<b>Salidas:</b><br>'
            '· <b>Excel</b>: tabla de puntos + hoja resumen por clase (media/min/max)<br>'
            '· <b>Gráfico PNG</b>: firma espectral con banda de incertidumbre (min–max) '
            'y línea de promedio por clase<br><br>'
            '<b>Nota Sentinel-2:</b> aplica factor de escala 0.0001 automáticamente.<br>'
            '<b>Nota:</b> el campo de clase debe contener el nombre de la cobertura '
            '(texto). Los puntos con la misma clase se promedian.'
        )

    # ── Parámetros ─────────────────────────────────────────────────────────────

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.RASTER,
                'Imagen multiespectral (reflectancia superficie)',
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.PUNTOS,
                'Capa de puntos de cobertura',
                types=[QgsProcessing.TypeVectorPoint],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CAMPO,
                'Campo de clase / cobertura',
                parentLayerParameterName=self.PUNTOS,
                type=QgsProcessingParameterField.String,
                defaultValue='Clase',
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.SENSOR,
                'Sensor / satélite',
                options=list(SENSORES.keys()),
                defaultValue=2,  # Landsat 8 por defecto
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.EXCEL,
                'Archivo Excel de salida (.xlsx)',
                fileFilter='Excel (*.xlsx)',
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.GRAFICO,
                'Gráfico PNG de salida',
                fileFilter='PNG (*.png)',
            )
        )

    # ── Procesamiento ──────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):

        # Importaciones diferidas
        try:
            import rasterio
        except ImportError:
            raise QgsProcessingException(
                'rasterio no encontrado.\n'
                'pip install rasterio --break-system-packages'
            )
        try:
            import geopandas as gpd
        except ImportError:
            raise QgsProcessingException(
                'geopandas no encontrado.\n'
                'pip install geopandas --break-system-packages'
            )
        try:
            import pandas as pd
        except ImportError:
            raise QgsProcessingException(
                'pandas no encontrado.\n'
                'pip install pandas openpyxl --break-system-packages'
            )
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches
        except ImportError:
            raise QgsProcessingException(
                'matplotlib no encontrado.\n'
                'pip install matplotlib --break-system-packages'
            )

        # Leer parámetros
        raster_layer  = self.parameterAsRasterLayer(parameters, self.RASTER, context)
        puntos_source = self.parameterAsSource(parameters, self.PUNTOS, context)
        campo_clase   = self.parameterAsString(parameters, self.CAMPO, context)
        sensor_idx    = self.parameterAsEnum(parameters, self.SENSOR, context)
        excel_out     = self.parameterAsFileOutput(parameters, self.EXCEL, context)
        grafico_out   = self.parameterAsFileOutput(parameters, self.GRAFICO, context)

        sensor_nombre = list(SENSORES.keys())[sensor_idx]
        cfg           = SENSORES[sensor_nombre]
        longitudes    = cfg['longitud']
        nombres_banda = cfg['bandas']
        factor_escala = cfg['factor']
        titulo        = cfg['titulo']

        raster_path = raster_layer.dataProvider().dataSourceUri().split('|')[0]

        # ── Verificar número de bandas ─────────────────────────────────────
        with rasterio.open(raster_path) as src:
            n_bandas_img = src.count
            crs_img      = src.crs

        n_bandas_sensor = len(nombres_banda)
        if n_bandas_img < n_bandas_sensor:
            raise QgsProcessingException(
                f'La imagen tiene {n_bandas_img} banda(s) pero el sensor '
                f'{sensor_nombre} requiere {n_bandas_sensor}.\n'
                'Verifica que sea la imagen correcta para el sensor seleccionado.'
            )
        if n_bandas_img > n_bandas_sensor:
            feedback.pushInfo(
                f'  ⚠ La imagen tiene {n_bandas_img} bandas; '
                f'se usarán las primeras {n_bandas_sensor} ({sensor_nombre}).'
            )

        # ── Exportar puntos a GeoDataFrame ────────────────────────────────
        feedback.pushInfo('📍 Leyendo puntos de cobertura...')
        import tempfile, shutil
        tmp_dir  = tempfile.mkdtemp(prefix='geom_firma_')
        tmp_gpkg = os.path.join(tmp_dir, 'puntos.gpkg')

        try:
            from qgis.core import QgsVectorFileWriter, QgsCoordinateTransformContext
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName   = 'GPKG'
            opts.fileEncoding = 'UTF-8'

            # Obtener la capa vectorial real para exportar
            puntos_layer = self.parameterAsVectorLayer(parameters, self.PUNTOS, context)
            QgsVectorFileWriter.writeAsVectorFormatV3(
                puntos_layer, tmp_gpkg, QgsCoordinateTransformContext(), opts
            )
            gdf = gpd.read_file(tmp_gpkg)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # Verificar campo de clase
        if campo_clase not in gdf.columns:
            raise QgsProcessingException(
                f'El campo "{campo_clase}" no existe en la capa de puntos.\n'
                f'Campos disponibles: {list(gdf.columns)}'
            )

        # Reproyectar si CRS difiere
        try:
            epsg_img = crs_img.to_epsg()
            if epsg_img and gdf.crs and gdf.crs.to_epsg() != epsg_img:
                gdf = gdf.to_crs(epsg=epsg_img)
                feedback.pushInfo(f'  🔄 Puntos reproyectados a EPSG:{epsg_img}')
        except Exception:
            pass

        feedback.pushInfo(f'  📊 {len(gdf)} punto(s) | {gdf[campo_clase].nunique()} clase(s)')

        # ── Extraer valores por punto ──────────────────────────────────────
        feedback.pushInfo(f'🔬 Extrayendo reflectancia...')
        feedback.setProgress(20)

        with rasterio.open(raster_path) as src:
            # Leer todas las bandas necesarias de una vez
            datos_img = {
                i+1: src.read(i+1).astype('float64')
                for i in range(n_bandas_sensor)
            }

            filas = []
            for _, punto in gdf.iterrows():
                geom = punto.geometry
                x, y = geom.x, geom.y
                try:
                    row, col = src.index(x, y)
                    clase = str(punto[campo_clase])[:80]
                    fila = {
                        'Clase': clase,
                        'Este':  round(x, 2),
                        'Norte': round(y, 2),
                    }
                    for band_idx, nb in enumerate(nombres_banda, start=1):
                        val = float(datos_img[band_idx][row, col]) * factor_escala
                        fila[nb] = val
                    filas.append(fila)
                except Exception as e:
                    feedback.pushInfo(f'  ⚠ Punto fuera de imagen: {e}')

        if not filas:
            raise QgsProcessingException(
                'Ningún punto coincide con la extensión de la imagen. '
                'Verifica que los puntos estén dentro del raster y en el mismo CRS.'
            )

        df = pd.DataFrame(filas)
        feedback.pushInfo(f'  ✅ {len(df)} punto(s) extraído(s) correctamente')
        feedback.setProgress(50)

        # ── Estadísticas por clase ─────────────────────────────────────────
        feedback.pushInfo('📈 Calculando estadísticas por clase...')
        cols_banda = nombres_banda

        resumen_rows = []
        for clase, grupo in df.groupby('Clase'):
            fila_res = {'Clase': clase, 'N_puntos': len(grupo)}
            for nb in cols_banda:
                vals = grupo[nb].dropna()
                fila_res[f'{nb}_media'] = round(vals.mean(), 6) if len(vals) else np.nan
                fila_res[f'{nb}_min']   = round(vals.min(),  6) if len(vals) else np.nan
                fila_res[f'{nb}_max']   = round(vals.max(),  6) if len(vals) else np.nan
            resumen_rows.append(fila_res)

        df_resumen = pd.DataFrame(resumen_rows)
        feedback.setProgress(65)

        # ── Exportar Excel ─────────────────────────────────────────────────
        feedback.pushInfo(f'💾 Exportando Excel: {os.path.basename(excel_out)}')
        try:
            with pd.ExcelWriter(excel_out, engine='openpyxl') as writer:
                # Hoja 1: datos crudos por punto
                df.to_excel(writer, sheet_name='Datos_por_punto', index=False)

                # Hoja 2: resumen por clase
                df_resumen.to_excel(writer, sheet_name='Resumen_por_clase', index=False)

                # Hoja 3: tabla pivot para el gráfico (media por clase × banda)
                pivot_media = df.groupby('Clase')[cols_banda].mean()
                pivot_media.to_excel(writer, sheet_name='Media_por_clase')

                pivot_min = df.groupby('Clase')[cols_banda].min()
                pivot_min.to_excel(writer, sheet_name='Min_por_clase')

                pivot_max = df.groupby('Clase')[cols_banda].max()
                pivot_max.to_excel(writer, sheet_name='Max_por_clase')

        except ImportError:
            raise QgsProcessingException(
                'openpyxl no encontrado.\n'
                'pip install openpyxl --break-system-packages'
            )
        feedback.setProgress(75)

        # ── Gráfico firma espectral ────────────────────────────────────────
        feedback.pushInfo('🖼 Generando gráfico...')

        clases_ordenadas = sorted(df['Clase'].unique())
        n_clases         = len(clases_ordenadas)

        fig, ax = plt.subplots(figsize=(11, 7))
        ax.set_facecolor('#f8f9fa')
        fig.patch.set_facecolor('white')

        leyenda_patches = []

        for i, clase in enumerate(clases_ordenadas):
            color = COLORES[i % len(COLORES)]
            grupo = df[df['Clase'] == clase][cols_banda]

            media = grupo.mean().values
            vmin  = grupo.min().values
            vmax  = grupo.max().values

            # Banda de incertidumbre min–max
            ax.fill_between(
                longitudes, vmin, vmax,
                alpha=0.18,
                color=color,
                linewidth=0,
            )
            # Línea de promedio
            ax.plot(
                longitudes, media,
                color=color,
                marker='o',
                markersize=6,
                linewidth=2,
                linestyle='-',
                zorder=3,
            )
            # Marcadores min y max con líneas punteadas
            ax.plot(
                longitudes, vmin,
                color=color,
                linestyle=':',
                linewidth=0.8,
                alpha=0.7,
                zorder=2,
            )
            ax.plot(
                longitudes, vmax,
                color=color,
                linestyle=':',
                linewidth=0.8,
                alpha=0.7,
                zorder=2,
            )

            n_pts = len(grupo)
            patch = mpatches.Patch(color=color, label=f'{clase}  (n={n_pts})')
            leyenda_patches.append(patch)

        # Anotaciones de banda sobre el eje X
        for j, (lon, nb) in enumerate(zip(longitudes, nombres_banda)):
            ax.axvline(x=lon, color='gray', linewidth=0.4, linestyle='--', alpha=0.5)
            ax.text(lon, ax.get_ylim()[0] if ax.get_ylim()[0] != 0 else -0.005,
                    nb.replace('SR_', '').replace('RS_', ''),
                    ha='center', va='top', fontsize=7, color='#555555', rotation=45)

        ax.set_title(titulo, fontsize=15, fontweight='bold', color='#2c3e50', pad=14)
        ax.set_xlabel('Longitud de onda (μm)', fontsize=12, labelpad=8)
        ax.set_ylabel('Reflectancia Superficial', fontsize=12, labelpad=8)

        ax.legend(
            handles=leyenda_patches,
            loc='upper right',
            fontsize=10,
            framealpha=0.9,
            edgecolor='#cccccc',
            title='Coberturas',
            title_fontsize=10,
        )

        # Nota de sombra en leyenda
        ax.text(
            0.01, 0.98,
            'Sombra: rango min–max  |  Línea: promedio',
            transform=ax.transAxes,
            fontsize=8,
            color='#888888',
            va='top',
        )

        ax.grid(True, linestyle='--', linewidth=0.5, alpha=0.6, color='#dddddd')
        ax.set_xlim(min(longitudes) - 0.05, max(longitudes) + 0.05)

        plt.tight_layout()
        plt.savefig(grafico_out, dpi=300, bbox_inches='tight', facecolor='white')
        plt.close()

        feedback.pushInfo(f'  ✅ Gráfico guardado: {os.path.basename(grafico_out)}')
        feedback.setProgress(100)
        feedback.pushInfo('\n🎉 Firma espectral completada')
        feedback.pushInfo(f'   Excel:   {excel_out}')
        feedback.pushInfo(f'   Gráfico: {grafico_out}')

        return {
            self.EXCEL:   excel_out,
            self.GRAFICO: grafico_out,
        }

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
