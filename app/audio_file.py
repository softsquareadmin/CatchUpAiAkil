"""Audio file input (M8): decode an uploaded mp3 / wav / m4a to 16 kHz mono PCM16 with ffmpeg. The upload is
written to a temporary file only for decoding (m4a needs a seekable input) and deleted straight after; nothing is
kept unless `store_audio` is on (the session then saves the decoded PCM and audits it)."""
import asyncio
import os
import shutil
import tempfile

EXTENSIONS = (".mp3", ".wav", ".m4a")
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
MAX_SECONDS = 3 * 3600  # AssemblyAI closes a streaming session after 3 hours
BYTES_PER_SECOND = 32000  # 16 kHz, 16-bit, mono


class AudioFileError(ValueError):
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def check_name(name: str) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in EXTENSIONS:
        raise AudioFileError(f"Use an mp3, wav or m4a file (got '{ext or 'no extension'}').")
    return ext


async def decode(data: bytes, name: str, ffmpeg: str | None = None) -> bytes:
    """PCM16 16 kHz mono bytes, or AudioFileError with a message the user can act on."""
    ext = check_name(name)
    ffmpeg = ffmpeg or ffmpeg_path()
    if not ffmpeg:
        raise AudioFileError("ffmpeg is not installed, so audio files cannot be read. Install ffmpeg and restart.")
    if not data:
        raise AudioFileError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise AudioFileError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-v", "error", "-i", tmp, "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
    finally:
        os.unlink(tmp)
    if proc.returncode != 0 or not out:
        detail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no audio found"]
        raise AudioFileError(f"Could not read the audio file: {detail[0][:200]}")
    if len(out) / BYTES_PER_SECOND > MAX_SECONDS:
        raise AudioFileError("The file is longer than 3 hours, the limit of one streaming session.")
    return out
