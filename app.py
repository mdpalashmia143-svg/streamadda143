import os
import uuid
import shutil
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="StreamAdda")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

streams = {}


class StreamCreate(BaseModel):
    name: str = "Live Stream"
    total_minutes: int
    platform: str
    stream_key: str
    backup_rtmp: str = ""


@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
async def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    return {
        "ok": True,
        "ffmpeg": ffmpeg_ok
    }


@app.post("/api/upload/video")
async def upload_video(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No video selected")

    allowed = {
        ".mp4", ".mkv", ".mov",
        ".avi", ".webm", ".m4v"
    }

    ext = Path(file.filename).suffix.lower()

    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format"
        )

    filename = f"{uuid.uuid4().hex}{ext}"
    save_path = UPLOAD_DIR / filename

    try:
        with open(save_path, "wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                buffer.write(chunk)

    except Exception as e:
        if save_path.exists():
            save_path.unlink()
        raise HTTPException(
            status_code=500,
            detail=f"Upload failed: {str(e)}"
        )

    return {
        "ok": True,
        "path": str(save_path),
        "filename": file.filename
    }


@app.get("/api/streams")
async def get_streams():
    result = {}

    for sid, stream in streams.items():
        result[sid] = {
            "id": sid,
            "name": stream["name"],
            "status": stream["status"],
            "filename": stream["filename"],
            "total_minutes": stream["total_minutes"],
            "platform": stream["platform"],
            "uptime": get_uptime(stream),
            "stop_at": stream.get("stop_at")
        }

    return result


@app.post("/api/streams")
async def create_stream(data: StreamCreate):

    if data.total_minutes < 2:
        raise HTTPException(
            status_code=400,
            detail="Minimum duration is 2 minutes"
        )

    if data.platform not in ["youtube", "facebook"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid platform"
        )

    if not data.stream_key.strip():
        raise HTTPException(
            status_code=400,
            detail="Stream key required"
        )

    video_path = None

    uploaded_files = sorted(
        UPLOAD_DIR.glob("*"),
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )

    if uploaded_files:
        video_path = uploaded_files[0]

    if not video_path or not video_path.exists():
        raise HTTPException(
            status_code=400,
            detail="Please upload a video first"
        )

    stream_id = uuid.uuid4().hex[:12]

    if data.platform == "youtube":
        rtmp_url = "rtmps://a.rtmp.youtube.com/live2"
    else:
        rtmp_url = "rtmps://live-api-s.facebook.com:443/rtmp"

    output_url = f"{rtmp_url}/{data.stream_key}"

    stream = {
        "id": stream_id,
        "name": data.name,
        "status": "starting",
        "filename": video_path.name,
        "video_path": str(video_path),
        "total_minutes": data.total_minutes,
        "platform": data.platform,
        "stream_key": data.stream_key,
        "backup_rtmp": data.backup_rtmp,
        "started_at": datetime.now(timezone.utc),
        "process": None,
        "logs": []
    }

    streams[stream_id] = stream

    asyncio.create_task(
        run_ffmpeg(stream_id, str(video_path), output_url, data.total_minutes)
    )

    return {
        "ok": True,
        "id": stream_id,
        "name": data.name,
        "status": "starting"
    }


async def run_ffmpeg(stream_id, video_path, output_url, total_minutes):

    stream = streams.get(stream_id)

    if not stream:
        return

    duration_seconds = total_minutes * 60

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",

        "-stream_loop", "-1",
        "-re",
        "-i", video_path,

        "-t", str(duration_seconds),

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "128k",

        "-f", "flv",
        output_url
    ]

    try:

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        stream["process"] = process
        stream["status"] = "online"

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            text = line.decode(
                "utf-8",
                errors="ignore"
            ).strip()

            if text:
                stream["logs"].append(text)

                if len(stream["logs"]) > 200:
                    stream["logs"].pop(0)

        await process.wait()

        if stream["status"] != "offline":
            stream["status"] = "offline"

    except Exception as e:

        stream["status"] = "error"
        stream["logs"].append(str(e))

    finally:

        stream["process"] = None


@app.post("/api/streams/{stream_id}/stop")
async def stop_stream(stream_id: str):

    stream = streams.get(stream_id)

    if not stream:
        raise HTTPException(
            status_code=404,
            detail="Stream not found"
        )

    process = stream.get("process")

    if process:

        try:
            process.terminate()

            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=10
                )

            except asyncio.TimeoutError:

                process.kill()
                await process.wait()

        except Exception:
            pass

    stream["status"] = "offline"

    return {
        "ok": True,
        "message": "Stream stopped"
    }


@app.delete("/api/streams/{stream_id}")
async def delete_stream(stream_id: str):

    stream = streams.get(stream_id)

    if not stream:
        raise HTTPException(
            status_code=404,
            detail="Stream not found"
        )

    process = stream.get("process")

    if process:

        try:
            process.terminate()
        except Exception:
            pass

    video_path = stream.get("video_path")

    if video_path:

        try:
            path = Path(video_path)

            if path.exists():
                path.unlink()

        except Exception:
            pass

    del streams[stream_id]

    return {
        "ok": True
    }


@app.patch("/api/streams/{stream_id}")
async def update_stream(stream_id: str, data: dict):

    stream = streams.get(stream_id)

    if not stream:
        raise HTTPException(
            status_code=404,
            detail="Stream not found"
        )

    if "stop_at" in data:
        stream["stop_at"] = data["stop_at"]

    return {
        "ok": True
    }


@app.get("/api/streams/{stream_id}/events")
async def stream_events(stream_id: str):

    stream = streams.get(stream_id)

    if not stream:
        raise HTTPException(
            status_code=404,
            detail="Stream not found"
        )

    logs = stream.get("logs", [])

    return {
        "logs": logs
    }


def get_uptime(stream):

    started = stream.get("started_at")

    if not started:
        return 0

    now = datetime.now(timezone.utc)

    return int(
        (now - started).total_seconds()
  )
