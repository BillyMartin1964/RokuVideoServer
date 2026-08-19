# Roku Video Server

A lightweight, modular Python 3 media server designed to index local and attached storage volumes, generate dynamic poster thumbnails, and stream video content to Roku client applications via SceneGraph nodes.

---

## Features

* **Modular Service Architecture:** Clean separation of concerns following a Service Layer Pattern with explicit Dependency Injection.
* **HTTP Range Request Support:** Smooth seeking and streaming via `video_service.py` using standard HTTP `206 Partial Content` headers.
* **Smart Thumbnail Generation:** Automated video thumbnail generation with fallback defaults and Roku-optimized HTTP caching (`Cache-Control`, `Expires`).
* **Multi-Drive & Directory Indexing:** Cross-platform volume scanning (`/Volumes` on macOS, drive letters on Windows) with support for Spotlight (`mdfind`) and fallback filesystem crawling.
* **Robust Socket Handling:** Built-in connection loss prevention handling `BrokenPipeError` and `ConnectionResetError` during client disconnects or media scrub probing.

---

## Architecture Overview

The application entry point (`server.py`) acts as the **Composition Root**, instantiating core services and injecting dependencies into the custom HTTP request router.
