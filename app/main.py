"""API del editor: POST /api/analyze recibe los audios de gameplay y webcam y
devuelve los tramos a conservar. Contrato en CONTRATO-CORTES.md (frontend).

    uvicorn app.main:app --host 0.0.0.0 --port 8000

--host 0.0.0.0 porque el frontend corre en Docker y llega por la red del
contenedor, no por 127.0.0.1.
"""
import traceback
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .analyze import AnalysisError, analyze

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Toda respuesta que no sea 200 lleva {"error": "mensaje"}, incluido el 422
# de validacion que FastAPI arma por default con otro formato.
@app.exception_handler(RequestValidationError)
def _validation_error(_request, _exc):
    return JSONResponse(status_code=422, content={"error": "Faltan datos para analizar el video."})


@app.exception_handler(AnalysisError)
def _analysis_error(_request, exc):
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


@app.exception_handler(Exception)
def _unexpected_error(_request, exc):
    traceback.print_exception(exc)
    return JSONResponse(status_code=500, content={"error": "Error inesperado al analizar el video."})


# def (no async): el analisis bloquea durante minutos y asi corre en el
# threadpool de FastAPI en vez de trabar el event loop.
@app.post("/api/analyze")
def analyze_endpoint(
    gameplay: Annotated[UploadFile, File()],
    webcam: Annotated[UploadFile, File()],
    webcamOffset: Annotated[float, Form(allow_inf_nan=False)],
):
    return analyze(gameplay.file, webcam.file, webcamOffset)
