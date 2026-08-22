#!/usr/bin/env python3

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

import api.directories as api_directories
import api.drives as api_drives
import api.thumbnails as api_thumbnails
import api.videos as api_videos
import config
from config import (
    CACHE_LOCK,
    PORT,
    log,
    log_separator,
)
from services import ffmpeg_service, thumbnail_service, video_service

# Capture server boot timestamp (Unix epoch time)
SERVER_START_TIME = time.time()


def get_local_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip:
            return ip
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def send_json_response(self, data, status=200):
        # Automatically inject start_time into any dictionary response if needed, 
        # or rely on api_videos.handle_get_health to include it.
        if isinstance(data, dict) and "start_time" not in data:
            data["start_time"] = SERVER_START_TIME
            
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def send_file_headers(
        self,
        file_path,
        file_size,
        status=200,
        content_length=None,
        content_range=None,
    ):
        content_type = video_service.get_video_format_info(file_path)["contentType"]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Length",
            str(content_length if content_length is not None else file_size),
        )
        self.send_header("Accept-Ranges", "bytes")

        if content_range:
            self.send_header("Content-Range", content_range)

        try:
            modified_time = os.path.getmtime(file_path)
            modified_string = time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(modified_time)
            )
            self.send_header("Last-Modified", modified_string)
        except Exception:
            pass

        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        url_path = unquote(urlparse(self.path).path)
        if url_path.startswith("/api/files/"):
            file_id = url_path.replace("/api/files/", "", 1).strip()
            with CACHE_LOCK:
                item = config.FILE_MAP.get(file_id)
                file_path = item.get("path") if item else None

            if not item or not file_path or not os.path.exists(file_path):
                self.send_error(404, "File Not Found")
                return

            api_videos.stream_video_file(self, file_path, send_body=False)
            return

        self.send_error(404, "Endpoint Not Found")

    def do_GET(self):
        start_time = time.time()
        parsed_url = urlparse(self.path)
        url_path = unquote(parsed_url.path)
        query_params = parse_qs(parsed_url.query)

        if url_path == "/api/health":
            api_videos.handle_get_health(self)
            return

        # Fetch USB Drives metadata
        if url_path == "/api/drives":
            api_drives.handle_get_drives(self)
            return

        # Fetch directories (handles all directories OR drive-filtered queries)
        if url_path == "/api/directories":
            drive_filter = query_params.get("drive", [None])[0]
            if drive_filter:
                api_directories.handle_get_directories_by_drive(self, drive_filter)
            else:
                api_directories.handle_get_all_directories(self)
            return

        # Explicit drive route: /api/directories/drive/<drive_name>
        if url_path.startswith("/api/directories/drive/"):
            drive_name = url_path.replace("/api/directories/drive/", "", 1).strip()
            api_directories.handle_get_directories_by_drive(self, drive_name)
            return

        if url_path.startswith("/api/files") and not url_path.startswith("/api/files/"):
            api_videos.handle_get_files(self, query_params, start_time)
            return

        if url_path.startswith("/api/thumbnails/"):
            file_id = url_path.replace("/api/thumbnails/", "", 1).strip()
            api_thumbnails.handle_get_thumbnail(self, file_id)
            return

        if url_path.startswith("/api/files/"):
            file_id = url_path.replace("/api/files/", "", 1).strip()
            with CACHE_LOCK:
                item = config.FILE_MAP.get(file_id)
                file_path = item.get("path") if item else None

            if not item or not file_path or not os.path.exists(file_path):
                self.send_error(404, "File Not Found")
                return

            api_videos.stream_video_file(self, file_path, send_body=True)
            return

        self.send_error(404, "Endpoint Not Found")

    def do_POST(self):
        url_path = unquote(self.path)
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0

        post_data = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            body = {}

        if url_path == "/api/files/move":
            api_videos.handle_move_file(self, body)
            return

        if url_path == "/api/files/rename":
            api_videos.handle_rename_file(self, body)
            return

        self.send_error(404, "Endpoint Not Found")

    def do_DELETE(self):
        url_path = unquote(self.path)
        if url_path.startswith("/api/files/"):
            file_id = url_path.replace("/api/files/", "", 1).strip()
            api_videos.handle_delete_file(self, file_id)
            return

        self.send_error(404, "Endpoint Not Found")


def run_server():
    log_separator()
    log("ROKU MEDIA HUB SERVER")
    log("Starting server...")
    log_separator()

    ffmpeg_service.initialize_ffmpeg()
    thumbnail_service.ensure_default_poster()
    video_service.load_disk_cache()

    timer_thread = threading.Thread(
        target=video_service.background_timer_loop,
        daemon=True,
        name="CatalogScanner",
    )
    timer_thread.start()

    server_address = ("", PORT)
    httpd = ThreadedHTTPServer(server_address, RequestHandler)
    local_ip = get_local_ip()

    log_separator()
    log("Roku Media Hub Server running on:")
    log(f"    http://{local_ip}:{PORT}")
    log(f"    http://127.0.0.1:{PORT}")
    log("")
    log("Health check:")
    log(f"    http://{local_ip}:{PORT}/api/health")
    log_separator()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\nShutting down server...")
        httpd.server_close()
        log("Server stopped.")


if __name__ == "__main__":
    run_server()