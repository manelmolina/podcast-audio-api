import hmac
import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
JOBS_DIR = DATA_DIR / "jobs"
INTRO_FILE = Path(os.environ.get("INTRO_FILE", str(DATA_DIR / "intro.mp3")))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
JOB_TTL_HOURS = int(os.environ.get("JOB_TTL_HOURS", "24"))
FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("FFMPEG_TIMEOUT_SECONDS", "14400"))
API_KEY = os.environ.get("API_KEY", "")
LOUDNESS_I = float(os.environ.get("LOUDNESS_I", "-16"))
LOUDNESS_TP = -1.5
LOUDNESS_LRA = 11.0
ALLOWED_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".mp4"}
PROCESS_LOCK = threading.Lock()
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024 * 2 + 10 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 1024 * 1024
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

if not -70.0 <= LOUDNESS_I <= -5.0:
    raise ValueError("LOUDNESS_I debe estar entre -70 y -5 LUFS.")


def error_response(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def cleanup_old_jobs() -> None:
    if JOB_TTL_HOURS <= 0 or not JOBS_DIR.exists():
        return
    cutoff = time.time() - JOB_TTL_HOURS * 3600
    for child in JOBS_DIR.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
        except OSError:
            app.logger.warning("Could not clean old job %s", child, exc_info=True)


def validate_upload(upload, label: str) -> str:
    if upload is None or not upload.filename:
        raise ValueError(f"Falta el audio que va {label} de la intro.")
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"El audio {label} tiene un formato no admitido. "
            f"Usa: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
        )
    return suffix


def edited_filename(original_filename: str) -> str:
    basename = Path(original_filename.replace("\\", "/")).name
    stem = Path(basename).stem.strip()
    stem = "".join(character for character in stem if character.isprintable() and character not in "/\\")
    stem = stem[:180].strip() or "audio"
    return f"{stem} - edited.mp3"


def run_ffmpeg(arguments: list[str], loglevel: str = "error") -> str:
    command = ["ffmpeg", "-hide_banner", "-loglevel", loglevel, "-nostdin", "-y", *arguments]
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        return result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("El procesamiento superó el tiempo máximo permitido.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "Error desconocido de FFmpeg").strip()[-1200:]
        raise RuntimeError(f"No se pudo procesar uno de los audios: {detail}") from exc


def audio_sample_rate(source: Path) -> int:
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate",
        "-of", "json",
        str(source),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        streams = json.loads(result.stdout).get("streams", [])
        return int(streams[0]["sample_rate"])
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("No se pudo detectar la frecuencia de muestreo del audio.") from exc


def denoise_if_eligible(source: Path, job_dir: Path) -> tuple[Path, Path | None]:
    if source.suffix.lower() != ".wav" or audio_sample_rate(source) != 48000:
        return source, None

    work_dir = job_dir / f"deepfilter-{source.stem}"
    output_dir = work_dir / "output"
    prepared = work_dir / "input.wav"
    output_dir.mkdir(parents=True)

    # Give DeepFilterNet a predictable 48 kHz, 16-bit PCM WAV regardless of the
    # particular WAV encoding uploaded by the client.
    run_ffmpeg(
        [
            "-i", str(source),
            "-map", "0:a:0",
            "-vn",
            "-ar", "48000",
            "-c:a", "pcm_s16le",
            str(prepared),
        ]
    )

    command = [
        "deep-filter",
        "--compensate-delay",
        "--output-dir", str(output_dir),
        str(prepared),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("La reducción de ruido superó el tiempo máximo permitido.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "Error desconocido de DeepFilterNet").strip()[-1200:]
        raise RuntimeError(f"DeepFilterNet no pudo limpiar el audio: {detail}") from exc

    enhanced = output_dir / prepared.name
    if not enhanced.is_file():
        raise RuntimeError("DeepFilterNet no generó el audio limpio esperado.")
    app.logger.info("DeepFilterNet aplicado al WAV de 48 kHz %s", source.name)
    return enhanced, work_dir


def loudness_filter(source: Path) -> str:
    analysis = run_ffmpeg(
        [
            "-i", str(source),
            "-map", "0:a:0",
            "-vn",
            "-af", f"loudnorm=I={LOUDNESS_I}:TP={LOUDNESS_TP}:LRA={LOUDNESS_LRA}:print_format=json",
            "-f", "null",
            "-",
        ],
        loglevel="info",
    )
    try:
        metrics = json.loads(analysis[analysis.rfind("{"): analysis.rfind("}") + 1])
        values = {
            "measured_I": float(metrics["input_i"]),
            "measured_TP": float(metrics["input_tp"]),
            "measured_LRA": float(metrics["input_lra"]),
            "measured_thresh": float(metrics["input_thresh"]),
            "offset": float(metrics["target_offset"]),
        }
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("Non-finite loudness measurement")
    except (ValueError, KeyError, json.JSONDecodeError):
        # Silence and extremely short clips can lack finite measurements. The
        # dynamic one-pass mode remains safe for those inputs.
        return f"loudnorm=I={LOUDNESS_I}:TP={LOUDNESS_TP}:LRA={LOUDNESS_LRA}"

    measured = ":".join(f"{key}={value}" for key, value in values.items())
    return (
        f"loudnorm=I={LOUDNESS_I}:TP={LOUDNESS_TP}:LRA={LOUDNESS_LRA}:"
        f"{measured}:linear=true"
    )


def normalize_audio(source: Path, destination: Path) -> None:
    # Two-pass EBU R128 normalization gives every segment the same perceived
    # loudness before they are concatenated.
    run_ffmpeg(
        [
            "-i", str(source),
            "-map", "0:a:0",
            "-vn",
            "-map_metadata", "-1",
            "-af", loudness_filter(source),
            "-ar", "44100",
            "-ac", "2",
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            str(destination),
        ]
    )


def concatenate(parts: list[Path], destination: Path, list_file: Path) -> None:
    list_file.write_text(
        "".join(f"file '{part.as_posix()}'\n" for part in parts), encoding="utf-8"
    )
    run_ffmpeg(
        [
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            "-id3v2_version", "3",
            str(destination),
        ]
    )


class CleanupIterable:
    def __init__(self, iterable, job_dir: Path):
        self.original = iterable
        self.iterator = iter(iterable)
        self.job_dir = job_dir
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        close = getattr(self.original, "close", None)
        if close:
            close()
        shutil.rmtree(self.job_dir, ignore_errors=True)


def remove_job_after_stream(response, job_dir: Path):
    response.response = CleanupIterable(response.response, job_dir)
    return response


@app.before_request
def require_api_key():
    if request.path in PUBLIC_PATHS:
        return None

    authorization = request.authorization
    basic_is_valid = bool(
        API_KEY
        and authorization
        and (authorization.type or "").lower() == "basic"
        and hmac.compare_digest(authorization.username or "", "admin")
        and hmac.compare_digest(authorization.password or "", API_KEY)
    )

    if request.path == "/ui" or request.path.startswith("/static/"):
        if basic_is_valid:
            return None
        return Response(
            "Autenticación requerida.",
            401,
            {"WWW-Authenticate": 'Basic realm="Podcast Audio", charset="UTF-8"'},
        )

    if not API_KEY:
        return None

    api_key_is_valid = hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY)
    if api_key_is_valid or basic_is_valid:
        return None
    return error_response("API key ausente o incorrecta.", 401)


@app.get("/")
def index():
    return jsonify(
        {
            "service": "podcast-audio-merge",
            "endpoints": {
                "/merge": "Une before + intro + after",
                "/merge-two": "Une intro + audio",
            },
            "frontend": "/ui",
            "documentation": "/docs",
            "multipart_fields": {
                "/merge": ["before", "after"],
                "/merge-two": ["audio"],
            },
            "output": "audio/mpeg",
            "intro_ready": INTRO_FILE.is_file(),
        }
    )


@app.get("/ui")
def frontend():
    response = make_response(render_template("index.html", loudness_i=f"{LOUDNESS_I:g}"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' https://cdn.jsdelivr.net; "
        "script-src 'self' https://code.jquery.com; "
        "connect-src 'self'; img-src 'self' data:;"
    )
    return response


@app.get("/docs")
def swagger_ui():
    return """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Podcast Audio API</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
  <script>
    window.onload = () => SwaggerUIBundle({
      url: '/openapi.json',
      dom_id: '#swagger-ui',
      deepLinking: true,
      persistAuthorization: true,
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
      layout: 'StandaloneLayout'
    });
  </script>
</body>
</html>"""


@app.get("/openapi.json")
def openapi_spec():
    audio_upload_schema = {
        "type": "object",
        "required": ["before", "after"],
        "properties": {
            "before": {
                "type": "string",
                "format": "binary",
                "description": "Audio que aparecerá al principio.",
            },
            "after": {
                "type": "string",
                "format": "binary",
                "description": "Audio que aparecerá al final.",
            },
        },
    }
    single_audio_schema = {
        "type": "object",
        "required": ["audio"],
        "properties": {
            "audio": {
                "type": "string",
                "format": "binary",
                "description": "Audio que se reproducirá después de intro.mp3.",
            }
        },
    }
    merge_responses = {
        "200": {
            "description": "MP3 resultante.",
            "content": {"audio/mpeg": {"schema": {"type": "string", "format": "binary"}}},
        },
        "400": {"description": "Falta un archivo o su formato no está permitido."},
        "401": {"description": "API key ausente o incorrecta."},
        "413": {"description": "La petición supera el límite de tamaño."},
        "422": {"description": "FFmpeg no pudo procesar uno de los audios."},
        "429": {"description": "Ya hay otro episodio procesándose."},
    }
    return jsonify(
        {
            "openapi": "3.0.3",
            "info": {
                "title": "Podcast Audio API",
                "version": "3.3.0",
                "description": (
                    "Reduce ruido con DeepFilterNet en WAV de 48 kHz, normaliza "
                    "la sonoridad y concatena los audios utilizando la intro configurada."
                ),
            },
            "components": {
                "securitySchemes": {
                    "ApiKeyAuth": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "X-API-Key",
                    }
                }
            },
            "security": [{"ApiKeyAuth": []}],
            "paths": {
                "/merge": {
                    "post": {
                        "summary": "Unir before + intro + after",
                        "description": "Inserta /data/intro.mp3 entre los dos audios enviados.",
                        "requestBody": {
                            "required": True,
                            "content": {"multipart/form-data": {"schema": audio_upload_schema}},
                        },
                        "responses": {
                            **merge_responses,
                            "503": {"description": "No se encuentra el archivo de intro."},
                        },
                    }
                },
                "/merge-two": {
                    "post": {
                        "summary": "Unir intro + audio",
                        "description": "Anteponer /data/intro.mp3 al único audio enviado.",
                        "requestBody": {
                            "required": True,
                            "content": {"multipart/form-data": {"schema": single_audio_schema}},
                        },
                        "responses": {
                            **merge_responses,
                            "503": {"description": "No se encuentra el archivo de intro."},
                        },
                    }
                },
                "/health": {
                    "get": {
                        "summary": "Comprobar el estado del servicio",
                        "security": [],
                        "responses": {
                            "200": {"description": "Servicio listo."},
                            "503": {"description": "La intro no está disponible."},
                        },
                    }
                },
            },
        }
    )


@app.post("/merge")
def process_audio():
    return merge_uploaded_audio(with_leading_audio=True)


@app.post("/merge-two")
def process_two_audios():
    return merge_uploaded_audio(with_leading_audio=False)


def merge_uploaded_audio(with_leading_audio: bool):
    cleanup_old_jobs()
    if not INTRO_FILE.is_file():
        return error_response("No se encuentra /data/intro.mp3.", 503)

    try:
        if with_leading_audio:
            before = request.files.get("before")
            after = request.files.get("after")
            before_suffix = validate_upload(before, "antes")
            after_suffix = validate_upload(after, "después")
        else:
            before = None
            before_suffix = None
            after = request.files.get("audio")
            after_suffix = validate_upload(after, "después")
        download_name = edited_filename(after.filename)
    except ValueError as exc:
        return error_response(str(exc), 400)

    # Keep one encoding job at a time, while a spare Gunicorn thread serves health checks.
    if not PROCESS_LOCK.acquire(blocking=False):
        return error_response("Ya hay un episodio procesándose. Reinténtalo más tarde.", 429)

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    before_source = job_dir / f"before{before_suffix}" if before_suffix else None
    after_source = job_dir / f"{'after' if with_leading_audio else 'audio'}{after_suffix}"
    output = job_dir / "episodio-final.mp3"

    try:
        job_dir.mkdir(parents=True, exist_ok=False)
        # Werkzeug spools multipart file bodies to temporary disk and save() copies
        # them in chunks; the full upload is never retained in Python memory.
        if before_source:
            before.save(before_source)
        after.save(after_source)
        max_bytes = MAX_UPLOAD_MB * 1024 * 1024
        uploaded_sources = [source for source in [before_source, after_source] if source]
        if any(source.stat().st_size > max_bytes for source in uploaded_sources):
            raise ValueError(f"Cada audio puede ocupar como máximo {MAX_UPLOAD_MB} MB.")

        processed_sources = {}
        deepfilter_dirs = []
        for source in uploaded_sources:
            processed, work_dir = denoise_if_eligible(source, job_dir)
            processed_sources[source] = processed
            if work_dir:
                deepfilter_dirs.append(work_dir)

        normalized = []
        if before_source:
            normalized.append(job_dir / "01-before.mp3")
            normalize_audio(processed_sources[before_source], normalized[-1])
        normalized.append(job_dir / ("02-intro.mp3" if before_source else "01-intro.mp3"))
        normalize_audio(INTRO_FILE, normalized[-1])
        normalized.append(job_dir / ("03-after.mp3" if before_source else "02-audio.mp3"))
        normalize_audio(processed_sources[after_source], normalized[-1])
        concatenate(normalized, output, job_dir / "concat.txt")

        for temporary in [*uploaded_sources, *normalized, job_dir / "concat.txt"]:
            temporary.unlink(missing_ok=True)
        for directory in deepfilter_dirs:
            shutil.rmtree(directory, ignore_errors=True)
    except (ValueError, RuntimeError) as exc:
        app.logger.warning("Job %s failed: %s", job_id, exc)
        shutil.rmtree(job_dir, ignore_errors=True)
        return error_response(str(exc), 422)
    except Exception:
        app.logger.exception("Unexpected failure in job %s", job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        return error_response("Error interno al crear el episodio.", 500)
    finally:
        PROCESS_LOCK.release()

    response = send_file(
        output,
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name=download_name,
        conditional=True,
    )
    return remove_job_after_stream(response, job_dir)


@app.get("/health")
def health():
    ready = INTRO_FILE.is_file()
    return {"status": "ok" if ready else "degraded", "intro": ready}, 200 if ready else 503


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    return error_response(
        f"La petición supera el límite configurado ({MAX_UPLOAD_MB} MB por archivo).", 413
    )


JOBS_DIR.mkdir(parents=True, exist_ok=True)
