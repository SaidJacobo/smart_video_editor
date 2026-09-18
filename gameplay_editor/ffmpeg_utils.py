"""Wrappers finos sobre ffprobe/ffmpeg: metadata, extraccion de audio, silencios."""
import json
import re
import subprocess

import numpy as np


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


def _parse_fps(rate_str):
    if "/" in rate_str:
        num, den = rate_str.split("/")
        den = float(den)
        return float(num) / den if den else 0.0
    return float(rate_str)


def video_info(path):
    data = probe(path)
    video_stream = next(s for s in data["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in data["streams"])
    duration = float(data["format"].get("duration") or video_stream.get("duration") or 0.0)
    fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "25/1")
    return {
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "fps": fps,
        "duration": duration,
        "has_audio": has_audio,
    }


def chain_metadata(path):
    """Metadata al estilo 'meta.media.*' que Kdenlive escribe en cada clip que
    agrega desde la GUI. Sin esto, un chain generado a mano queda con muchos
    menos props que uno real y Kdenlive tiene que reprobar todo en el momento
    de abrir el proyecto -- justo el codepath donde vimos crashear la app."""
    data = probe(path)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)

    props = {}
    idx = 0
    if video:
        fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "25/1")
        props[f"meta.media.{idx}.stream.type"] = "video"
        props[f"meta.media.{idx}.stream.frame_rate"] = f"{fps:g}"
        props[f"meta.media.{idx}.stream.sample_aspect_ratio"] = "1"
        props[f"meta.media.{idx}.codec.width"] = video.get("width", "")
        props[f"meta.media.{idx}.codec.height"] = video.get("height", "")
        props[f"meta.media.{idx}.codec.rotate"] = "0"
        props[f"meta.media.{idx}.codec.pix_fmt"] = video.get("pix_fmt", "yuv420p")
        props[f"meta.media.{idx}.codec.sample_aspect_ratio"] = "1"
        props[f"meta.media.{idx}.codec.colorspace"] = "709"
        props[f"meta.media.{idx}.codec.name"] = video.get("codec_name", "")
        props[f"meta.media.{idx}.codec.long_name"] = video.get("codec_long_name", "")
        idx += 1
    if audio:
        props[f"meta.media.{idx}.stream.type"] = "audio"
        props[f"meta.media.{idx}.codec.sample_fmt"] = audio.get("sample_fmt", "fltp")
        props[f"meta.media.{idx}.codec.sample_rate"] = audio.get("sample_rate", "48000")
        props[f"meta.media.{idx}.codec.channels"] = audio.get("channels", "2")
        props[f"meta.media.{idx}.codec.name"] = audio.get("codec_name", "")
        props[f"meta.media.{idx}.codec.long_name"] = audio.get("codec_long_name", "")
        idx += 1
    props["meta.media.nb_streams"] = str(idx)
    props["meta.media.width"] = video.get("width", "") if video else ""
    props["meta.media.height"] = video.get("height", "") if video else ""
    props["meta.media.progressive"] = "1"
    return props



def extract_mono_pcm(path, sample_rate=4000):
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", path,
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-f", "s16le", "-",
        ],
        check=True,
        capture_output=True,
    )
    samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sample_rate


_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silence(path, noise_db=-30.0, min_silence_sec=0.7):
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "info", "-i", path, "-vn", "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    log = proc.stderr
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(log)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(log)]
    # silence_end siempre viene acompanado de su silence_start; si el archivo
    # termina en silencio, ffmpeg no emite silence_end -> lo cerramos a mano.
    if len(ends) < len(starts):
        duration = video_info(path)["duration"]
        ends.append(duration)
    return list(zip(starts, ends))
