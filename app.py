"""
Spanify — servidor Flask.

Acepta archivos .epub o .pdf por POST /traducir, los traduce al español
de España con Google Gemini y permite descargar el resultado.
"""

import threading
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, send_file

from translator.common import (
    UPLOADS_DIR,
    APIKeyError,
    CuotaAgotadaError,
    TraductorError,
)

app = Flask(__name__, static_folder="static", static_url_path="")

jobs: dict[str, dict] = {}


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/traducir", methods=["POST"])
def traducir():
    if "file" not in request.files:
        return jsonify({"error": "Falta el archivo"}), 400

    archivo = request.files["file"]
    filename = archivo.filename or ""
    ext = Path(filename).suffix.lower()

    if ext == ".epub":
        from translator.epub import translate_epub as translate_fn
    elif ext == ".pdf":
        from translator.pdf import translate_pdf as translate_fn
    else:
        return jsonify({"error": "Formato no soportado. Sube un archivo .epub o .pdf"}), 400

    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "Falta la API key de Gemini"}), 400

    saved_path = UPLOADS_DIR / Path(filename).name
    archivo.save(saved_path)

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "status": "en_curso",
        "progreso": 0,
        "mensaje": "Iniciando...",
        "archivo_salida": None,
        "error": None,
        "nombre_original": filename,
    }

    thread = threading.Thread(
        target=_run_translation,
        args=(job_id, saved_path, api_key, translate_fn),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


def _run_translation(job_id: str, path: Path, api_key: str, translate_fn) -> None:
    def cb(pct: int, msg: str) -> None:
        jobs[job_id]["progreso"] = pct
        jobs[job_id]["mensaje"] = msg

    try:
        output = translate_fn(str(path), api_key=api_key, progress_cb=cb)
        jobs[job_id].update({
            "status": "completado",
            "progreso": 100,
            "mensaje": "¡Traducción completada!",
            "archivo_salida": str(output),
        })
    except APIKeyError as e:
        jobs[job_id].update({"status": "error", "error": f"API key inválida: {e}"})
    except CuotaAgotadaError as e:
        jobs[job_id].update({
            "status": "error",
            "error": str(e),
            "mensaje": str(e),
        })
    except TraductorError as e:
        jobs[job_id].update({"status": "error", "error": str(e)})
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


@app.route("/estado/<job_id>")
def estado(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    return jsonify(jobs[job_id])


@app.route("/descargar/<job_id>")
def descargar(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Trabajo no encontrado"}), 404

    job = jobs[job_id]
    if job["status"] != "completado" or not job["archivo_salida"]:
        return jsonify({"error": "El archivo todavía no está listo"}), 400

    output_path = Path(job["archivo_salida"])
    return send_file(str(output_path), as_attachment=True, download_name=output_path.name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=False)
