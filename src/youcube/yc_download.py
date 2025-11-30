#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modernized YouCube downloader with SABR bypass, multi-client fallback,
format recovery, and robust error handling.
No changes to external API or Lua expectations.
"""

# Built-in
from asyncio import run_coroutine_threadsafe
from os import getenv, listdir
from os.path import abspath, dirname, join
from tempfile import TemporaryDirectory

# Local modules
from yc_colours import RESET, Foreground
from yc_logging import NO_COLOR, YTDLPLogger, logger
from yc_magic import run_with_live_output
from yc_spotify import SpotifyURLProcessor
from yc_utils import (
    cap_width_and_height,
    create_data_folder_if_not_present,
    get_audio_name,
    get_video_name,
    is_audio_already_downloaded,
    is_video_already_downloaded,
    remove_ansi_escape_codes,
    remove_whitespace,
)

# Optional JSON
try:
    from orjson import dumps
except ModuleNotFoundError:
    from json import dumps

# External modules
from sanic import Websocket
from yt_dlp import YoutubeDL

# Constants
DATA_FOLDER = join(dirname(abspath(__file__)), "data")
FFMPEG_PATH = getenv("FFMPEG_PATH", "ffmpeg")
SANJUUNI_PATH = getenv("SANJUUNI_PATH", "sanjuuni")
DISABLE_OPENCL = bool(getenv("DISABLE_OPENCL"))


# ---------------------------------------------------------------------------
# Conversion Helpers
# ---------------------------------------------------------------------------

def download_video(temp_dir: str, media_id: str, resp: Websocket, loop, width: int, height: int):
    """ Convert downloaded video into 32vid """
    run_coroutine_threadsafe(
        resp.send(dumps({"action": "status", "message": "Converting video to 32vid ..."})),
        loop,
    )

    prefix = "[Sanjuuni]" if NO_COLOR else f"{Foreground.BRIGHT_YELLOW}[Sanjuuni]{RESET} "

    def handler(line):
        logger.debug("%s%s", prefix, line)
        run_coroutine_threadsafe(resp.send(dumps({"action": "status", "message": line})), loop)

    returncode = run_with_live_output(
        [
            SANJUUNI_PATH,
            f"--width={width}",
            f"--height={height}",
            "-i",
            join(temp_dir, listdir(temp_dir)[0]),
            "--raw",
            "-o",
            join(DATA_FOLDER, get_video_name(media_id, width, height)),
            "--disable-opencl" if DISABLE_OPENCL else "",
        ],
        handler,
    )

    if returncode != 0:
        logger.warning("Sanjuuni exited with %s", returncode)
        run_coroutine_threadsafe(
            resp.send(dumps({"action": "error", "message": "Failed to convert video!"})),
            loop,
        )


def download_audio(temp_dir: str, media_id: str, resp: Websocket, loop):
    """ Convert downloaded audio into dfpwm """
    run_coroutine_threadsafe(
        resp.send(dumps({"action": "status", "message": "Converting audio to dfpwm ..."})),
        loop,
    )

    prefix = "[FFmpeg]" if NO_COLOR else f"{Foreground.BRIGHT_GREEN}[FFmpeg]{RESET} "

    def handler(line):
        logger.debug("%s%s", prefix, line)

    returncode = run_with_live_output(
        [
            FFMPEG_PATH,
            "-i",
            join(temp_dir, listdir(temp_dir)[0]),
            "-f", "dfpwm",
            "-ar", "48000",
            "-ac", "1",
            join(DATA_FOLDER, get_audio_name(media_id)),
        ],
        handler,
    )

    if returncode != 0:
        logger.warning("FFmpeg exited with %s", returncode)
        run_coroutine_threadsafe(
            resp.send(dumps({"action": "error", "message": "Failed to convert audio!"})),
            loop,
        )

# ---------------------------------------------------------------------------
# Modernized Youtube Extractor Logic
# ---------------------------------------------------------------------------

ANDROID_UA = "com.google.android.youtube/19.20.34 (Linux; U; Android 13)"
WEB_EMBED_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/118 Safari/537.36"
MUSIC_UA = "com.google.android.apps.youtube.music/6.26.52"


def build_client_profiles(is_video: bool, temp_dir: str, hook):
    """Return a list of yt-dlp option dicts (multi-client fallback)."""
    base_format = (
        "bv*[ext=mp4][height<=360]+ba[ext=m4a]/"
        "bv*+ba/bestvideo+bestaudio/bestaudio/best"
        if is_video else
        "ba[ext=m4a]/bestaudio"
    )

    return [
        # Android client (best SABR bypass)
        {
            "user_agent": ANDROID_UA,
            "http_headers": {"User-Agent": ANDROID_UA},
            "format": base_format,
            "outtmpl": join(temp_dir, "%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "default_search": "auto",
            "extract_flat": "in_playlist",
            "progress_hooks": [hook],
            "logger": YTDLPLogger(),
            "youtube_include_dash_manifest": True,
            "geo_bypass": True,
        },
        # Web embedded fallback
        {
            "user_agent": WEB_EMBED_UA,
            "http_headers": {"User-Agent": WEB_EMBED_UA},
            "format": base_format,
            "outtmpl": join(temp_dir, "%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "default_search": "auto",
            "extract_flat": "in_playlist",
            "progress_hooks": [hook],
            "logger": YTDLPLogger(),
            "youtube_include_dash_manifest": True,
            "geo_bypass": True,
        },
        # YouTube Music fallback (audio-focused)
        {
            "user_agent": MUSIC_UA,
            "http_headers": {"User-Agent": MUSIC_UA},
            "format": base_format,
            "outtmpl": join(temp_dir, "%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "default_search": "auto",
            "extract_flat": "in_playlist",
            "progress_hooks": [hook],
            "logger": YTDLPLogger(),
            "youtube_include_dash_manifest": True,
            "geo_bypass": True,
        }
    ]


def try_extract_with_profiles(url, profiles):
    """Try multiple clients to bypass SABR and get usable info."""
    last_error = None

    for profile in profiles:
        try:
            yt = YoutubeDL(profile)
            return yt, yt.extract_info(url, download=False)
        except Exception as e:
            last_error = e
            logger.warning("Extractor failed with client: %s", profile.get("user_agent"))
            continue

    raise last_error  # rethrow final failure


# ---------------------------------------------------------------------------
# Main Download Logic
# ---------------------------------------------------------------------------

def download(
    url: str,
    resp: Websocket,
    loop,
    width: int,
    height: int,
    spotify_url_processor: SpotifyURLProcessor,
):
    """Modern robust downloader w/ SABR bypass + multi-client fallback."""

    is_video = width is not None and height is not None

    if width and height:
        width, height = cap_width_and_height(width, height)

    # Status hook
    def my_hook(info):
        if info.get("status") == "downloading":
            msg = remove_ansi_escape_codes(
                f"download {remove_whitespace(info.get('_percent_str'))} ETA {info.get('_eta_str')}"
            )
            run_coroutine_threadsafe(
                resp.send(dumps({"action": "status", "message": msg})),
                loop,
            )

    # --------------------------------------------------------------
    # Spotify resolution
    # --------------------------------------------------------------
    playlist_videos = []
    if spotify_url_processor:
        processed = spotify_url_processor.auto(url)
        if processed:
            if isinstance(processed, list):
                url = spotify_url_processor.auto(processed[0])
                playlist_videos = processed[1:]
            else:
                url = processed

    # --------------------------------------------------------------
    # Extraction using multi-client fallback
    # --------------------------------------------------------------
    run_coroutine_threadsafe(
        resp.send(dumps({"action": "status", "message": "Getting resource information ..."})),
        loop,
    )

    with TemporaryDirectory(prefix="youcube-") as temp_dir:

        # build client profiles
        profiles = build_client_profiles(is_video, temp_dir, my_hook)

        try:
            yt_dl, data = try_extract_with_profiles(url, profiles)
        except Exception as e:
            logger.error("Extractor failed: %s", str(e))
            run_coroutine_threadsafe(
                resp.send(dumps({"action": "error", "message": "Failed to extract media info"})),
                loop,
            )
            return {"action": "error", "message": "Extractor failed"}, []

        # Generic extractor fallback ID
        if data.get("extractor") == "generic":
            data["id"] = "g" + data.get("webpage_url_domain") + data.get("id")

        # Playlist support
        if data.get("_type") == "playlist":
            for v in data["entries"]:
                playlist_videos.append(v.get("id"))
            playlist_videos = playlist_videos[1:]
            data = data["entries"][0]

        # Missing metadata recovery
        if data.get("extractor") == "youtube" and (
            data.get("view_count") is None or data.get("like_count") is None
        ):
            try:
                data = yt_dl.extract_info(data.get("id"), download=False)
            except Exception:
                pass

        media_id = data.get("id")

        if data.get("is_live"):
            return {"action": "error", "message": "Livestreams are not supported"}, []

        create_data_folder_if_not_present()

        audio_exists = is_audio_already_downloaded(media_id)
        video_exists = is_video_already_downloaded(media_id, width, height)

        # ----------------------------------------------------------
        # Download actual resource
        # ----------------------------------------------------------
        if not audio_exists or (is_video and not video_exists):
            run_coroutine_threadsafe(
                resp.send(dumps({"action": "status", "message": "Downloading resource ..."})),
                loop,
            )

            try:
                yt_dl.process_ie_result(data, download=True)
            except Exception as e:
                logger.error("Download failure: %s", str(e))
                run_coroutine_threadsafe(
                    resp.send(dumps({"action": "error", "message": "Failed to download media"})),
                    loop,
                )
                return {"action": "error", "message": "Download failure"}, []

        if not audio_exists:
            download_audio(temp_dir, media_id, resp, loop)

        if is_video and not video_exists:
            download_video(temp_dir, media_id, resp, loop, width, height)

    # --------------------------------------------------------------
    # Build output (Lua client compatibility guaranteed)
    # --------------------------------------------------------------
    out = {
        "action": "media",
        "id": media_id,
        "title": data.get("title"),
        "like_count": data.get("like_count"),
        "view_count": data.get("view_count"),
    }

    if playlist_videos:
        out["playlist_videos"] = playlist_videos

    files = [get_audio_name(media_id)]
    if is_video:
        files.append(get_video_name(media_id, width, height))

    return out, files
