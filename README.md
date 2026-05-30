---
title: Spanify
emoji: 📚
colorFrom: orange
colorTo: green
sdk: docker
pinned: false
---

# Spanify

Traductor de documentos **EPUB** y **PDF** al **español de España** usando la API de **Google Gemini**.

Detecta automáticamente el tipo de archivo por su extensión, preserva al máximo el formato original y mantiene un checkpoint persistente entre sesiones para que las traducciones largas puedan reanudarse si se cortan.

## Uso

1. Abre la app y pega tu API key de [Google AI Studio](https://aistudio.google.com/app/apikey). Se guarda únicamente en el `localStorage` del navegador.
2. Arrastra (o selecciona) un archivo `.epub` o `.pdf`.
3. Pulsa **Traducir**. La página muestra el progreso en tiempo real.
4. Al terminar, el archivo traducido se descarga automáticamente (`<nombre>_es.epub` o `<nombre>_es.pdf`).

## Despliegue

La app está pensada para [Hugging Face Spaces](https://huggingface.co/spaces) con SDK `docker`. El archivo `Dockerfile` arranca Flask en el puerto `7860` y persiste los checkpoints en el directorio `/data` de Spaces.

En local: `pip install -r requirements.txt && python app.py` (puerto 7860, datos en `./data`).

## Desarrollo y tests

Instala además las dependencias de test (no se despliegan en producción):

```
pip install -r requirements-dev.txt
```

Ejecuta la batería de tests:

```
pytest tests/ -q
```

### Modo mock: probar sin gastar API

Para probar el flujo completo en local **sin llamar a Gemini**, activa el
traductor simulado con la variable de entorno `SPANIFY_MOCK_TRANSLATE`. En ese
modo el texto se reinserta **sin traducir** (sale en el idioma original): sirve
para verificar al instante que la reconstrucción del PDF/EPUB es correcta —
texto colocado, imágenes intactas, tamaño normal— antes de gastar cuota.

PowerShell (Windows):

```powershell
$env:SPANIFY_MOCK_TRANSLATE = "1"; python app.py
# desactivar: Remove-Item Env:SPANIFY_MOCK_TRANSLATE   (o abre otra terminal)
```

bash:

```bash
SPANIFY_MOCK_TRANSLATE=1 python app.py
```

Notas:

- La app sigue pidiendo una API key en la interfaz aunque estés en modo mock;
  pega **cualquier texto** en ese campo (no se usa).
- Con el modo mock el resultado sale en el idioma original (es lo esperado:
  confirma que la reconstrucción funciona). Quita la variable y reinicia para la
  traducción real con Gemini.

## Arquitectura

```
app.py                      ← servidor Flask
translator/
  common.py                 ← cliente Gemini, reintentos, chunking, excepciones
  prompts.py                ← prompts externalizados (EPUB, PDF, contexto previo)
  epub.py                   ← traductor EPUB (HTML/XHTML)
  pdf.py                    ← traductor PDF (PyMuPDF)
  fonts.py                  ← fuentes sustitutas para reinsertar texto
  fonts/                    ← Noto Serif/Sans embebidas (subsets latinos, OFL)
static/
  index.html                ← frontend (HTML + CSS + JS en un archivo)
tests/                      ← pytest (pytest tests/) + verify_fixes.py (verificación visual)
Dockerfile
requirements.txt
```

Directorio de datos (no va al repo): `/data/uploads/` y `/data/checkpoints/`.

### Fuentes en el PDF traducido

Al reinsertar el texto traducido **no se reutiliza la fuente original** del PDF:
sus subsets suelen ser `Type0/Identity-H` sin mapa Unicode estándar y, al
escribir texto nuevo con ellos, el resultado sale ilegible en visores reales
(glifos desplazados, texto no copiable). En su lugar se usa siempre una fuente
**Noto embebida** elegida por el estilo del span (serif/sans + negrita/cursiva,
deducido de sus `flags`), que se embebe con codificación correcta y se ve igual
en cualquier visor. Las fuentes (`translator/fonts/*.ttf`, subsets latinos de
Noto, licencia OFL en `OFL.txt`) viajan con la app: no hay dependencias extra.

### Ajuste de texto que se alarga

El español ocupa de media bastante más que el inglés, así que un fragmento
traducido puede no caber donde estaba el original y pisar el de al lado. El
ajuste tiene dos partes:

1. **Reducción de tamaño global y uniforme.** Como el texto traducido casi
   siempre crece, se reduce el tamaño de **todo** el documento por un mismo
   factor (`GLOBAL_SIZE_FACTOR`, por defecto un 20 %). Así la altura de la letra
   es **uniforme entre líneas** (no se encoge una línea sí y otra no) y, al
   partir de un tamaño menor, casi nunca hace falta el paso siguiente.
2. **Condensado horizontal solo donde haga falta.** Para cada fragmento se
   calcula el **hueco real disponible**: la distancia hasta el obstáculo más
   cercano a su derecha, sea otro texto de su **misma banda vertical** (en
   cualquier parte de la página, no solo dentro de la misma línea de PyMuPDF),
   una imagen, o el borde de la página. Si tras la reducción aún no cabe, se
   **condensa horizontalmente** lo justo (hasta un 50 %), sin volver a tocar el
   tamaño. En expansiones extremas se acepta un mínimo desborde antes que romper
   la uniformidad de tamaño.

   Mirar en **toda la banda** (y no solo dentro de la línea) importa porque el
   original a menudo coloca palabras contiguas en líneas/bloques separados a la
   misma altura (p. ej. "Templar" y "Commandery", o el número y la descripción
   de una tabla); antes el texto traducido las pisaba al crecer.

Así el texto no desborda ni se solapa, con una sola línea por fragmento, altura
uniforme y sin tocar imágenes ni posiciones.

Además, las **imágenes se tratan como obstáculos**: cuando el original rodea una
imagen acortando líneas (algo que no queda registrado en el texto), el hueco
disponible se recorta hasta el borde izquierdo de esa imagen, de modo que el
texto traducido se condensa en lugar de meterse encima. Los fondos a página
completa se ignoran (el texto va sobre ellos a propósito). *Limitación actual:
solo se detectan imágenes rasterizadas (`get_image_info`); las figuras
vectoriales todavía no cuentan como obstáculo.*

## Mejoras futuras

Pendientes de implementar, conscientemente fuera del alcance actual:

- **Detección de idioma fuente**: una llamada previa a Gemini con una muestra del documento para detectar el idioma original y mostrárselo al usuario antes de iniciar la traducción. El idioma destino seguiría siendo siempre español de España.
- **Selector de modelo**: obtener la lista de modelos disponibles desde la API de Gemini y permitir elegir uno desde el frontend.
- **Contador de uso de API**: cuando Google exponga un endpoint de uso, mostrar peticiones consumidas y disponibles.
- **Reflujo de párrafo en PDF**: hoy, cuando el español se alarga, el texto se *condensa* para caber en su hueco (ver "Ajuste de texto que se alarga"). Eso evita solapes pero mantiene una sola línea por fragmento. Pendiente: para expansiones muy grandes, repartir el texto en varias líneas dentro del área del párrafo (reflow real), teniendo en cuenta los párrafos contiguos para no empujarlos verticalmente.
- **Fidelidad tipográfica exacta**: hoy el texto serif/sans del original se reemplaza por Noto Serif/Sans (no por la fuente exacta original, que no se puede reutilizar de forma portable). Se podría mapear familias concretas a sustitutas más parecidas (p. ej. Garamond → EB Garamond) o ampliar el juego de fuentes embebidas (mono, etc.).
