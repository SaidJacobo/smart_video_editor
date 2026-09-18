"""Conversion helpers between seconds/frames and Kdenlive's HH:MM:SS.mmm timecode."""


def seconds_to_frame(seconds: float, fps: float) -> int:
    return round(seconds * fps)


def frame_to_seconds(frame: int, fps: float) -> float:
    return frame / fps


def frame_to_tc(frame: int, fps: float) -> str:
    total_ms = round(frame * 1000 / fps)
    h, total_ms = divmod(total_ms, 3600_000)
    m, total_ms = divmod(total_ms, 60_000)
    s, ms = divmod(total_ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def seconds_to_tc(seconds: float, fps: float) -> str:
    return frame_to_tc(seconds_to_frame(seconds, fps), fps)


def fps_to_rational(fps: float):
    if abs(fps - round(fps)) < 0.01:
        return round(fps), 1
    return round(fps * 1000), 1000
