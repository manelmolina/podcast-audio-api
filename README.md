# Podcast Audio API

API y frontend web para montar episodios de audio. Permite crear:

- `intro.mp3 + audio`
- `audio 1 + intro.mp3 + audio 2`

Los WAV a 48 kHz se limpian con [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet). Todos los fragmentos se normalizan al nivel configurado (−16 LUFS de forma predeterminada) y el resultado se descarga como MP3.

## Puesta en marcha

Requisitos: Docker y Docker Compose.

```bash
cp .env.example .env
```

Edita `.env`, define una clave segura en `API_KEY` y ejecuta:

```bash
docker compose up -d
```

El servicio queda disponible en `http://localhost:8080`:

- Frontend: `/ui`
- Swagger: `/docs`
- OpenAPI: `/openapi.json`
- Estado: `/health`

El frontend usa autenticación HTTP Basic: usuario `admin` y como contraseña el valor de `API_KEY`. La API también admite la cabecera `X-API-Key`.

## Configuración

Las variables principales se encuentran en `compose.yml`:

| Variable | Valor predeterminado | Descripción |
| --- | --- | --- |
| `API_KEY` | Obligatoria | Clave de acceso a la API y contraseña del frontend |
| `LOUDNESS_I` | `-16` | Sonoridad objetivo en LUFS (entre −70 y −5) |
| `MAX_UPLOAD_MB` | `2048` | Tamaño máximo de subida en MB |
| `FFMPEG_TIMEOUT_SECONDS` | `14400` | Tiempo máximo de procesamiento |

`intro.mp3` se monta como archivo de solo lectura. Los resultados temporales permanecen dentro del contenedor y se eliminan al finalizar o interrumpir la descarga.

## API

### Intro + audio

```bash
curl -X POST http://localhost:8080/merge-two \
  -H "X-API-Key: TU_CLAVE" \
  -F "audio=@audio.mp3" \
  --output "audio - edited.mp3"
```

### Audio 1 + intro + audio 2

```bash
curl -X POST http://localhost:8080/merge \
  -H "X-API-Key: TU_CLAVE" \
  -F "before=@audio-1.mp3" \
  -F "after=@audio-2.mp3" \
  --output "audio-2 - edited.mp3"
```

La especificación completa y los formatos admitidos pueden consultarse en Swagger (`/docs`).

## Construcción local

```bash
docker build -t podcast-audio-api .
```

La imagen publicada que usa el Compose es `molinaig/podcast-audio-api:latest`.
