# Project Context: Roku Video Server Refactoring

**Goal:**
Finalize the refactoring of the monolithic Python 3 video server application into a robust, modular, service-oriented architecture. The main goal is completing the dependency injection setup in the main entry point (`server.py`) so all decoupled services initialize and orchestrate correctly for full runtime stability.

**Architectural Pattern & Core Technologies:**
* **Architecture:** Service Layer Pattern with explicit Dependency Injection. Business logic is isolated into specialized modules (`services/`), with `server.py` serving as the Composition Root.
* **Core Tech:** Python 3 (`http.server`, `socketserver`, `threading`, `shutil`).
* **Cross-Platform Target:** Windows/macOS backend serving media endpoints to Roku client applications (Roku SceneGraph SG nodes).

**Current Status of Modules (Stable & Complete):**
1. **`services/config.py`**: Centralized configuration and global thread locking (`CACHE_LOCK`, `PORT`, `VOLUMES_DIR`, `THUMB_CACHE_DIR`).
2. **`services/video_service.py`**: Handles media streaming with HTTP Range Request headers and `ffprobe` metadata extraction.
3. **`services/thumbnail_service.py`**: Handles dynamic thumbnail generation, default poster fallbacks, HEAD request validation, and Roku-friendly HTTP caching headers (`Cache-Control`, `Expires`).
4. **`services/media_catalog_service.py`**: Manages media catalog indexing (`mdfind` Spotlight / directory crawling), volume traversal safety, path normalization, and disk cache persistence.
5. **`services/request_handler.py`**: Serves as the HTTP API router and endpoint handler (drives, directory listings, media thumbnails, streaming) with strict connection drop handling (`BrokenPipeError`, `ConnectionResetError`).

**The Critical Next Step (Focus Area):**
* **File:** `server.py`
* **Task:** Finalize and validate the `run_server()` function acting as the **Composition Root**:
  1. Instantiating all core services (`MediaCatalogService`, `VideoService`, `ThumbnailService`) in proper dependency order.
  2. Passing the injected `server_context` / service mapping directly to `RequestHandler` instances.
  3. Diagnosing and resolving the runtime startup failure (exit code 1) during server startup and background catalog thread initialization.

**Summary of Progress:**
* **Completed:** Full modular decomposition, request handler routing contracts, thread-safe volume/directory reading, path normalization (`/` vs `\`), and robust thumbnail HTTP stream handling.
* **Pending:** Debugging and runtime validation of `server.py` execution to fix the exit code 1 crash on startup.