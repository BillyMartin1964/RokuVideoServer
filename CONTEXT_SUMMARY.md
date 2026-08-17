# Project Context: Roku Video Server Refactoring

**Goal:**
To finalize the refactoring of the monolithic video serving application into a robust, modular, and service-oriented architecture using Python 3. The primary objective is to complete the dependency injection setup within the main server entry point (`server.py`), ensuring all decoupled services are correctly initialized and orchestrated to make the application fully functional and runnable.

**Architectural Pattern & Core Technologies:**
*   **Architecture:** Service Layer Pattern with explicit Dependency Injection. All business logic is isolated into specialized service modules, and the main application entry point acts as the Composition Root.
*   **Core Tech:** Python 3 (Utilizing `http.server` and `socketserver`).
*   **Environment Notes:** The development is taking place on Windows, although some media utilities (like `mdfind`) are noted as targeting macOS paths, which is a current point of attention.

**Current Status of Modules (Stable & Complete):**
1.  **`services/config.py`**: Centralized configuration. Stable source for all global constants (e.g., `PORT`, `VOLUMES_DIR`, `THUMB_CACHE_DIR`).
2.  **`services/video_service.py`**: Handles core media logic, including `send_video_file` (streaming with Range Headers) and metadata retrieval (`ffprobe`). This service is functionally complete.
3.  **`services/media_catalog_service.py`**: Manages the content catalog. Fully implemented to handle both `mdfind` (Spotlight) and OS directory crawling, with robust persistence via disk caching.
4.  **`services/request_handler.py`**: Acts as the HTTP router. Its methods (`do_GET`, `do_HEAD`) are correctly structured to accept and utilize service dependencies (e.g., `catalog_service` in `get_catalog_file`), confirming the service interface contract.

**The Critical Next Step (Focus Area):**
*   **File:** `server.py`
*   **Task:** Finalize the `run_server()` function. This function must act as the **Composition Root**, responsible for:
    1.  Correctly instantiating all necessary services (`MediaCatalogService`, `VideoService`, etc.) in the correct initialization order.
    2.  Building and passing a cohesive `server_context` (or equivalent dependency map) to the `RequestHandler` instance.
    3.  Starting the `http.server` loop gracefully while ensuring background tasks (like the catalog scanning thread) are initiated and managed correctly.

**Summary of Completion:**
*   **Completed:** Architecture decomposition, Service implementation, Request routing contract definition. The dependency structure between `server.py` $\rightarrow$ `MediaCatalogService` $\rightarrow$ `VideoService` is architecturally complete and has been implemented.
*   **Pending:** Runtime validation and debugging of the Composition Root in `server.py`. The latest attempt to run the server failed with an exit code 1, indicating a runtime dependency or initialization error that must be resolved.