import streamlit as st
import zipfile
import os
import tempfile
import io
from lxml import etree
import ezdxf
from ezdxf.enums import TextEntityAlignment

# ─────────────────────────────────────────────
# Página y estilo
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="KMZ → DXF Converter",
    page_icon="📐",
    layout="centered"
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Inter:wght@300;400;600&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

.main-title {
    font-family: 'Space Mono', monospace;
    font-size: 2.2rem;
    font-weight: 700;
    color: #0f172a;
    letter-spacing: -1px;
    margin-bottom: 0;
}

.subtitle {
    font-family: 'Inter', sans-serif;
    font-weight: 300;
    color: #64748b;
    font-size: 1rem;
    margin-top: 4px;
    margin-bottom: 2rem;
}

.badge {
    display: inline-block;
    background: #0f172a;
    color: #f8fafc;
    font-family: 'Space Mono', monospace;
    font-size: 0.7rem;
    padding: 2px 8px;
    border-radius: 3px;
    margin-right: 6px;
}

.info-box {
    background: #f1f5f9;
    border-left: 3px solid #0f172a;
    padding: 12px 16px;
    border-radius: 0 6px 6px 0;
    font-size: 0.88rem;
    color: #475569;
    margin-bottom: 1rem;
}

.layer-item {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 6px;
    font-family: 'Space Mono', monospace;
    font-size: 0.78rem;
    color: #334155;
}

.success-box {
    background: #f0fdf4;
    border-left: 3px solid #22c55e;
    padding: 12px 16px;
    border-radius: 0 6px 6px 0;
    font-size: 0.88rem;
    color: #166534;
    margin: 1rem 0;
}

hr.divider {
    border: none;
    border-top: 1px solid #e2e8f0;
    margin: 1.5rem 0;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────
st.markdown('<p class="main-title">KMZ → DXF</p>', unsafe_allow_html=True)
st.markdown('<p class="subtitle">Convierte archivos KMZ de Google Earth a formato DXF para AutoCAD</p>', unsafe_allow_html=True)

st.markdown("""
<div class="info-box">
    <span class="badge">PUNTOS</span>
    <span class="badge">LÍNEAS</span>
    <span class="badge">POLÍGONOS</span>
    <span class="badge">ETIQUETAS</span>
    Cada carpeta del KMZ se convierte en una capa separada en DXF
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Namespace KML — detección automática
# ─────────────────────────────────────────────
# Algunos KMZ usan namespace distinto o ninguno.
# Detectamos el namespace real del archivo.

def get_namespace(root):
    """Extrae el namespace del elemento raíz, si existe."""
    tag = root.tag
    if tag.startswith("{"):
        return tag.split("}")[0] + "}"
    return ""

def tag(name, ns=""):
    return f"{ns}{name}"

# ─────────────────────────────────────────────
# Parsear coordenadas desde texto KML
# ─────────────────────────────────────────────
def parse_coords(coord_text):
    """Convierte texto 'lon,lat,alt lon,lat,alt ...' a lista de tuplas (x, y)"""
    points = []
    for token in coord_text.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                points.append((lon, lat))
            except ValueError:
                pass
    return points

# ─────────────────────────────────────────────
# Procesar cada Placemark
# ─────────────────────────────────────────────
def process_placemark(placemark, msp, layer_name, stats, ns):
    """Lee un Placemark y lo agrega al modelspace DXF"""
    
    # Nombre del placemark (para etiqueta)
    name_el = placemark.find(tag("name", ns))
    name = name_el.text.strip() if name_el is not None and name_el.text else ""

    # ── Punto ──────────────────────────────────
    point_el = placemark.find(f".//{tag('Point', ns)}/{tag('coordinates', ns)}")
    if point_el is not None and point_el.text:
        coords = parse_coords(point_el.text)
        if coords:
            x, y = coords[0]
            msp.add_point((x, y, 0), dxfattribs={"layer": layer_name})
            if name:
                msp.add_text(
                    name,
                    dxfattribs={
                        "layer": layer_name,
                        "height": 0.0001,
                        "insert": (x, y, 0),
                    }
                )
            stats["puntos"] += 1

    # ── Línea ──────────────────────────────────
    line_el = placemark.find(f".//{tag('LineString', ns)}/{tag('coordinates', ns)}")
    if line_el is not None and line_el.text:
        coords = parse_coords(line_el.text)
        if len(coords) >= 2:
            msp.add_lwpolyline(
                coords,
                dxfattribs={"layer": layer_name}
            )
            stats["lineas"] += 1

    # ── Polígono ────────────────────────────────
    poly_el = placemark.find(
        f".//{tag('Polygon', ns)}//{tag('outerBoundaryIs', ns)}//{tag('coordinates', ns)}"
    )
    if poly_el is not None and poly_el.text:
        coords = parse_coords(poly_el.text)
        if len(coords) >= 3:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            msp.add_lwpolyline(
                coords,
                close=True,
                dxfattribs={"layer": layer_name}
            )
            stats["poligonos"] += 1

# ─────────────────────────────────────────────
# Recorrer Folders recursivamente
# ─────────────────────────────────────────────
def process_folder(folder, msp, parent_name, stats, layer_map, ns):
    """Procesa una carpeta KML y sus subcarpetas"""
    name_el = folder.find(tag("name", ns))
    folder_name = name_el.text.strip() if name_el is not None and name_el.text else parent_name

    # Nombre de capa limpio para DXF (máx 31 chars, sin caracteres especiales)
    layer_name = folder_name[:31].replace("/", "-").replace("\\", "-").replace(":", "-")
    if not layer_name:
        layer_name = "SIN_NOMBRE"

    layer_map[layer_name] = layer_map.get(layer_name, 0)

    # Placemarks directos en esta carpeta
    for placemark in folder.findall(tag("Placemark", ns)):
        process_placemark(placemark, msp, layer_name, stats, ns)

    # Subcarpetas
    for subfolder in folder.findall(tag("Folder", ns)):
        process_folder(subfolder, msp, layer_name, stats, layer_map, ns)

# ─────────────────────────────────────────────
# Función principal de conversión
# ─────────────────────────────────────────────
def kmz_to_dxf(kmz_bytes):
    stats = {"puntos": 0, "lineas": 0, "poligonos": 0}
    layer_map = {}

    # Extraer KML del KMZ (que es un ZIP)
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as z:
        kml_files = [f for f in z.namelist() if f.endswith(".kml")]
        if not kml_files:
            raise ValueError("No se encontró ningún archivo .kml dentro del KMZ.")
        kml_content = z.read(kml_files[0])

    # Parsear KML
    root = etree.fromstring(kml_content)

    # Detectar namespace automáticamente
    ns = get_namespace(root)

    # Crear documento DXF
    doc = ezdxf.new(dxfversion="R2010")
    msp = doc.modelspace()

    # Procesar contenido del KML
    document_el = root.find(tag("Document", ns))
    container = document_el if document_el is not None else root

    # Placemarks en raíz
    for placemark in container.findall(tag("Placemark", ns)):
        process_placemark(placemark, msp, "GENERAL", stats, ns)

    # Carpetas
    for folder in container.findall(tag("Folder", ns)):
        process_folder(folder, msp, "GENERAL", stats, layer_map, ns)

    # Guardar DXF en memoria (ezdxf escribe texto, no bytes)
    output = io.StringIO()
    doc.write(output)
    dxf_str = output.getvalue()
    # Convertir a bytes para la descarga
    dxf_bytes = dxf_str.encode("utf-8")

    # Obtener capas reales usadas (desde las entidades)
    used_layers = sorted(set(
        e.dxf.layer for e in msp
        if hasattr(e.dxf, "layer") and e.dxf.layer not in ("0", "Defpoints")
    ))
    return dxf_bytes, stats, used_layers

# ─────────────────────────────────────────────
# UI - Upload
# ─────────────────────────────────────────────
st.markdown('<hr class="divider">', unsafe_allow_html=True)

uploaded_file = st.file_uploader(
    "Selecciona tu archivo KMZ",
    type=["kmz"],
    help="El archivo debe ser un KMZ exportado desde Google Earth o similar"
)

if uploaded_file:
    st.markdown(f"**Archivo cargado:** `{uploaded_file.name}` ({uploaded_file.size / 1024:.1f} KB)")
    
    output_name = uploaded_file.name.replace(".kmz", ".dxf").replace(".KMZ", ".dxf")

    if st.button("⚙️ Convertir a DXF", type="primary", use_container_width=True):
        with st.spinner("Procesando geometrías..."):
            try:
                dxf_bytes, stats, layers = kmz_to_dxf(uploaded_file.read())

                # Resumen
                st.markdown(f"""
                <div class="success-box">
                    ✅ <strong>Conversión exitosa</strong> — 
                    {stats['puntos']} puntos · 
                    {stats['lineas']} líneas · 
                    {stats['poligonos']} polígonos
                </div>
                """, unsafe_allow_html=True)

                # Capas generadas
                if layers:
                    st.markdown("**Capas generadas en el DXF:**")
                    for ln in layers:
                        st.markdown(f'<div class="layer-item">▸ {ln}</div>', unsafe_allow_html=True)

                # Botón de descarga
                st.download_button(
                    label=f"⬇️ Descargar {output_name}",
                    data=dxf_bytes,
                    file_name=output_name,
                    mime="application/dxf",
                    use_container_width=True
                )

            except Exception as e:
                st.error(f"❌ Error durante la conversión: {str(e)}")
                st.info("Verifica que el archivo KMZ no esté dañado y contenga geometrías válidas.")

# ─────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────
st.markdown('<hr class="divider">', unsafe_allow_html=True)
st.markdown("""
<div style="font-size:0.78rem; color:#94a3b8; font-family:'Space Mono', monospace;">
Librerías: lxml · ezdxf · streamlit &nbsp;|&nbsp; Flo Networks GIS Tools
</div>
""", unsafe_allow_html=True)
