"""
Audio mixer — sum mic + system WAV files into a single mixed WAV.

The capture-audio Swift binary writes the mic and system-audio tracks to two
separate 16kHz mono Int16 WAVs. Proper mixing (sample-accurate sum with
saturation) must happen before transcription. This is a temporary design —
see ROADMAP.md "In-Swift audio mixer" for the long-term fix.
"""

from __future__ import annotations

import logging
import os
import wave

logger = logging.getLogger(__name__)


def system_path_for(mic_path: str) -> str:
    """Return the sibling system-audio path for a given mic-audio path."""
    base, ext = os.path.splitext(mic_path)
    return f"{base}.sys{ext}"


def _read_pcm(path: str) -> tuple[bytes, int, int, int]:
    with wave.open(path, "rb") as wf:
        return (
            wf.readframes(wf.getnframes()),
            wf.getframerate(),
            wf.getnchannels(),
            wf.getsampwidth(),
        )


def mix_to(mic_path: str, system_path: str, out_path: str) -> bool:
    """
    Mix mic + system WAVs (16kHz mono Int16) and write the sum to out_path.

    Uses saturating addition so hot mic + hot system audio doesn't wrap.
    Returns True on success, False if the inputs are unusable.
    """
    if not os.path.exists(mic_path):
        logger.warning("mix_to: mic WAV missing: %s", mic_path)
        return False

    mic_pcm, mic_sr, mic_ch, mic_sw = _read_pcm(mic_path)

    if not os.path.exists(system_path):
        # Fall back to mic-only output (copy PCM unchanged).
        logger.info("mix_to: system WAV missing, writing mic-only mix")
        _write_pcm(out_path, mic_pcm, mic_sr, mic_ch, mic_sw)
        return True

    sys_pcm, sys_sr, sys_ch, sys_sw = _read_pcm(system_path)

    if (mic_sr, mic_ch, mic_sw) != (sys_sr, sys_ch, sys_sw):
        logger.warning(
            "mix_to: format mismatch mic=%s sys=%s — writing mic-only",
            (mic_sr, mic_ch, mic_sw), (sys_sr, sys_ch, sys_sw),
        )
        _write_pcm(out_path, mic_pcm, mic_sr, mic_ch, mic_sw)
        return True

    if mic_sw != 2:
        logger.warning("mix_to: expected 16-bit samples, got %d bytes — mic-only", mic_sw)
        _write_pcm(out_path, mic_pcm, mic_sr, mic_ch, mic_sw)
        return True

    mixed = _saturating_add_int16(mic_pcm, sys_pcm)
    _write_pcm(out_path, mixed, mic_sr, mic_ch, mic_sw)
    return True


def _write_pcm(path: str, pcm: bytes, sr: int, ch: int, sw: int) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(sw)
        wf.setframerate(sr)
        wf.writeframes(pcm)


def _saturating_add_int16(a: bytes, b: bytes) -> bytes:
    # Pad the shorter buffer with silence so both streams end together.
    try:
        import numpy as np
    except ImportError:  # pragma: no cover — numpy ships with the venv
        return _saturating_add_int16_pure(a, b)

    mic = np.frombuffer(a, dtype=np.int16)
    sysn = np.frombuffer(b, dtype=np.int16)
    n = max(mic.size, sysn.size)
    if mic.size < n:
        mic = np.concatenate([mic, np.zeros(n - mic.size, dtype=np.int16)])
    if sysn.size < n:
        sysn = np.concatenate([sysn, np.zeros(n - sysn.size, dtype=np.int16)])
    summed = mic.astype(np.int32) + sysn.astype(np.int32)
    np.clip(summed, -32768, 32767, out=summed)
    return summed.astype(np.int16).tobytes()


def _saturating_add_int16_pure(a: bytes, b: bytes) -> bytes:
    import struct
    n = max(len(a), len(b))
    if len(a) < n:
        a = a + b"\x00" * (n - len(a))
    if len(b) < n:
        b = b + b"\x00" * (n - len(b))
    samples = []
    for i in range(0, n, 2):
        sa = struct.unpack_from("<h", a, i)[0]
        sb = struct.unpack_from("<h", b, i)[0]
        s = sa + sb
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        samples.append(s)
    return struct.pack(f"<{len(samples)}h", *samples)
