"""
Lógica común a los traductores EPUB y PDF: cliente Gemini, reintentos,
chunking y excepciones del dominio.
"""

import os
import re
import time
from pathlib import Path

from google import genai
from google.genai import types


# ── Configuración ──────────────────────────────────────────────────────────────

GEMINI_MODEL    = "gemini-3.1-flash-lite"
MAX_CHUNK_CHARS = 8000
REQUEST_DELAY   = 5
MAX_RETRIES     = 10
TEMPERATURE     = 0.2
TOP_P           = 0.95
PREV_CTX_CHARS  = 800


# ── Excepciones ────────────────────────────────────────────────────────────────

class TraductorError(Exception):
    """Error genérico del traductor."""


class APIKeyError(TraductorError):
    """La API key no se ha proporcionado o no es válida."""


class CuotaAgotadaError(TraductorError):
    """La cuota diaria de Gemini está agotada."""


# ── Directorios persistentes ───────────────────────────────────────────────────

DATA_DIR = Path("/data") if Path("/data").exists() else Path("./data")
UPLOADS_DIR = DATA_DIR / "uploads"
CHECKPOINTS_DIR = DATA_DIR / "checkpoints"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)


# ── Cliente Gemini ─────────────────────────────────────────────────────────────

def setup_gemini(api_key: str | None = None):
    """Inicializa y devuelve el cliente Gemini. Lanza APIKeyError si la key está vacía."""
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    if not key:
        raise APIKeyError("No se ha proporcionado una API key de Google Gemini.")
    try:
        return genai.Client(api_key=key)
    except Exception as e:
        raise APIKeyError(f"API key inválida: {e}") from e


def mock_translate_enabled() -> bool:
    """
    True si la variable de entorno SPANIFY_MOCK_TRANSLATE está activa.

    En modo mock los traductores NO llaman a Gemini: devuelven el texto sin
    traducir. Sirve para probar la reconstrucción del documento (colocación de
    texto, imágenes, tamaño) en local y sin gastar cuota de API. Se lee en cada
    llamada para poder activarlo/desactivarlo sin reimportar.
    """
    return bool(os.environ.get("SPANIFY_MOCK_TRANSLATE"))


# ── Utilidades de respuesta ────────────────────────────────────────────────────

def strip_markdown_fences(text: str) -> str:
    """Quita los fences ``` que a veces añade el modelo alrededor de la respuesta."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


_NUMBERED_TOKEN_RE = re.compile(r"\[\[\s*(\d+)\s*\]\]")


def parse_numbered_response(resp: str, count: int) -> dict[int, str]:
    """
    Parsea una respuesta con el protocolo numerado ([[N]] traducción) y devuelve
    {índice: traducción} emparejando por NÚMERO, no por posición: si el modelo
    fusiona, divide o se salta fragmentos, el resto no se desplaza. Tolera
    traducciones multilínea (el contenido de cada [[N]] llega hasta el
    siguiente marcador). Los índices fuera de [0, count) y los repetidos se
    ignoran.
    """
    result: dict[int, str] = {}
    matches = list(_NUMBERED_TOKEN_RE.finditer(resp))
    for pos, m in enumerate(matches):
        idx = int(m.group(1))
        end = matches[pos + 1].start() if pos + 1 < len(matches) else len(resp)
        if 0 <= idx < count and idx not in result:
            result[idx] = resp[m.end():end].strip()
    return result


def _parse_retry_delay(error: Exception) -> float | None:
    match = re.search(r"retry[^\d]*(\d+(?:\.\d+)?)s", str(error), re.IGNORECASE)
    return float(match.group(1)) + 2 if match else None


def _is_daily_quota_exhausted(error: Exception) -> bool:
    msg = str(error)
    return "PerDay" in msg or "per_day" in msg.lower()


# ── Envío de chunks ────────────────────────────────────────────────────────────

def send_chunk(client, prompt: str) -> str:
    """
    Envía un prompt a Gemini con bucle de reintentos y backoff.

    - CuotaAgotadaError si la cuota diaria está agotada.
    - El resto de errores reintenta con espera progresiva hasta MAX_RETRIES.
    """
    config = types.GenerateContentConfig(
        temperature=TEMPERATURE,
        top_p=TOP_P,
    )

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
            )
            return strip_markdown_fences(response.text)
        except CuotaAgotadaError:
            raise
        except Exception as e:
            last_error = e
            if _is_daily_quota_exhausted(e):
                raise CuotaAgotadaError(
                    "Cuota diaria de la API agotada. "
                    "El progreso queda guardado: vuelve a ejecutar mañana."
                ) from e
            wait = _parse_retry_delay(e) or (attempt + 1) * 10
            print(f"    ⚠ Error (intento {attempt + 1}/{MAX_RETRIES}): esperando {wait:.0f}s...")
            time.sleep(wait)

    raise TraductorError(f"Fallo persistente tras {MAX_RETRIES} reintentos: {last_error}")


# ── Chunking ───────────────────────────────────────────────────────────────────

def split_into_chunks(texts: list[str], max_chars: int) -> list[list[str]]:
    """
    Agrupa una lista de strings en sublistas cuyo tamaño total (sumando len)
    no supere max_chars. Los strings sueltos más largos que max_chars
    se devuelven en su propia sublista.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for text in texts:
        tlen = len(text)
        if current and current_len + tlen > max_chars:
            chunks.append(current)
            current = [text]
            current_len = tlen
        else:
            current.append(text)
            current_len += tlen
    if current:
        chunks.append(current)
    return chunks
