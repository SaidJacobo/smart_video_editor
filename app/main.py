"""API del editor: POST /api/analyze recibe los audios de gameplay y webcam y
devuelve los tramos a conservar. Contrato en CONTRATO-CORTES.md (frontend).
POST /api/export/kdenlive arma el .kdenlive del timeline del editor
(SPEC-EXPORT-KDENLIVE.md).

    uvicorn app.main:app --host 0.0.0.0 --port 8000

--host 0.0.0.0 porque el frontend corre en Docker y llega por la red del
contenedor, no por 127.0.0.1.
"""
import traceback
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from . import export_kdenlive
from .analyze import AnalysisError, analyze

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# (422, 500) de cada endpoint
_MENSAJES = {
    "/api/analyze": ("Faltan datos para analizar el video.", "Error inesperado al analizar el video."),
    "/api/export/kdenlive": ("Datos inválidos para exportar.", "Error inesperado al exportar."),
}


# Toda respuesta que no sea 200 lleva {"error": "mensaje"}, incluido el 422
# de validacion que FastAPI arma por default con otro formato.
@app.exception_handler(RequestValidationError)
def _validation_error(request, _exc):
    return JSONResponse(status_code=422, content={"error": _MENSAJES[request.url.path][0]})


@app.exception_handler(AnalysisError)
def _analysis_error(_request, exc):
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


@app.exception_handler(Exception)
def _unexpected_error(request, exc):
    traceback.print_exception(exc)
    return JSONResponse(status_code=500, content={"error": _MENSAJES[request.url.path][1]})


# def (no async): el analisis bloquea durante minutos y asi corre en el
# threadpool de FastAPI en vez de trabar el event loop.
@app.post("/api/analyze")
def analyze_endpoint(
    gameplay: Annotated[UploadFile, File()],
    webcam: Annotated[UploadFile, File()],
    webcamOffset: Annotated[float, Form(allow_inf_nan=False)],
):
    return analyze(gameplay.file, webcam.file, webcamOffset)


class AudioInfo(BaseModel):
    sampleRate: int = Field(gt=0)
    channels: int = Field(gt=0)


class VideoFile(BaseModel):
    name: str = Field(min_length=1)
    duration: float = Field(gt=0, allow_inf_nan=False)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0, allow_inf_nan=False)
    audio: AudioInfo | None


class WebcamFile(VideoFile):
    offset: float = Field(allow_inf_nan=False)


class ExportMedia(BaseModel):
    id: str
    file: VideoFile
    webcam: WebcamFile | None


class ExportClip(BaseModel):
    mediaId: str
    in_: float = Field(alias="in", ge=0, allow_inf_nan=False)
    out: float = Field(allow_inf_nan=False)


class ExportRequest(BaseModel):
    media: list[ExportMedia]
    clips: list[ExportClip] = Field(min_length=1)

    @model_validator(mode="after")
    def _clips_validos(self):
        media_ids = {m.id for m in self.media}
        if any(c.mediaId not in media_ids or c.out <= c.in_ for c in self.clips):
            raise ValueError("clip invalido")
        return self


@app.post("/api/export/kdenlive")
def export_kdenlive_endpoint(request: ExportRequest):
    return Response(export_kdenlive.build(request), media_type="application/xml")
