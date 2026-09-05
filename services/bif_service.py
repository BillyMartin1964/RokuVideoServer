import os
import shutil
import subprocess
import tempfile

import config
from config import log

BIFTOOL_PATH = None
BIFTOOL_TIMEOUT_SECONDS = 3600


def find_biftool():
    candidates = [
        "/opt/homebrew/bin/biftool",
        "/usr/local/bin/biftool",
        "/usr/bin/biftool",
        "/opt/local/bin/biftool",
    ]

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return shutil.which("biftool")


def test_biftool(biftool_path):
    if not biftool_path:
        return False

    try:
        result = subprocess.run(
            [biftool_path, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode == 0:
            log("--> biftool test successful.")
            return True

        log(f"<!> biftool exists but returned exit code {result.returncode}")

    except (subprocess.SubprocessError, OSError, ValueError) as ex:
        log(f"<!> biftool executable test failed: {type(ex).__name__}: {ex}")

    return False


def initialize_biftool():
    global BIFTOOL_PATH

    BIFTOOL_PATH = find_biftool()

    if not BIFTOOL_PATH:
        log("<!> biftool NOT FOUND. BIF generation is disabled.")
        return False

    log(f"--> Found biftool binary at: {BIFTOOL_PATH}")

    if test_biftool(BIFTOOL_PATH):
        log("--> biftool is ready for BIF generation.")
        return True

    BIFTOOL_PATH = None
    log("<!> biftool was found but could not be executed.")

    return False


def get_bif_path(file_id):
    return os.path.join(
        config.BIF_CACHE_DIR,
        f"{file_id}.bif",
    )


def generate_bif(file_id, video_path):
    if not BIFTOOL_PATH:
        log(f"<!> BIF generation skipped because biftool is unavailable: {video_path}")
        return False

    if not file_id:
        log(f"<!> BIF generation skipped because video ID is empty: {video_path}")
        return False

    if not video_path or not os.path.isfile(video_path):
        log(
            f"<!> BIF generation skipped because video file does not exist: {video_path}"
        )
        return False

    try:
        os.makedirs(config.BIF_CACHE_DIR, exist_ok=True)

        final_bif_path = get_bif_path(file_id)

        if os.path.isfile(final_bif_path) and os.path.getsize(final_bif_path) > 0:
            return True

        with tempfile.TemporaryDirectory(
            prefix=f"bif_{file_id}_",
            dir=config.BIF_CACHE_DIR,
        ) as temp_directory:
            result = subprocess.run(
                [
                    BIFTOOL_PATH,
                    video_path,
                ],
                cwd=temp_directory,
                capture_output=True,
                text=True,
                timeout=BIFTOOL_TIMEOUT_SECONDS,
                check=False,
            )

            if result.returncode != 0:
                log(
                    f"<!> biftool failed for {os.path.basename(video_path)} "
                    f"with exit code {result.returncode}"
                )

                if result.stderr:
                    log(f"<!> biftool stderr: {result.stderr.strip()}")

                return False

            bif_files = []

            for file_name in os.listdir(temp_directory):
                if not file_name.lower().endswith(".bif"):
                    continue

                file_path = os.path.join(
                    temp_directory,
                    file_name,
                )

                if os.path.isfile(file_path):
                    bif_files.append(file_path)

            if not bif_files:
                log(
                    f"<!> biftool completed successfully but produced no BIF files "
                    f"for {os.path.basename(video_path)}"
                )
                return False

            fhd_bif = None

            for bif_file in bif_files:
                if "-fhd.bif" in os.path.basename(bif_file).lower():
                    fhd_bif = bif_file
                    break

            if fhd_bif is None:
                for bif_file in bif_files:
                    if "-hd.bif" in os.path.basename(bif_file).lower():
                        fhd_bif = bif_file
                        break

            if fhd_bif is None:
                fhd_bif = bif_files[0]

            if not os.path.isfile(fhd_bif) or os.path.getsize(fhd_bif) == 0:
                log(
                    f"<!> biftool produced an invalid BIF file "
                    f"for {os.path.basename(video_path)}"
                )
                return False

            temporary_final_path = final_bif_path + ".tmp"

            shutil.copy2(
                fhd_bif,
                temporary_final_path,
            )

            if not os.path.isfile(temporary_final_path):
                log(
                    f"<!> Failed to create temporary BIF file for "
                    f"{os.path.basename(video_path)}"
                )
                return False

            if os.path.getsize(temporary_final_path) == 0:
                try:
                    os.remove(temporary_final_path)
                except OSError:
                    pass

                log(
                    f"<!> Temporary BIF file is empty for "
                    f"{os.path.basename(video_path)}"
                )
                return False

            os.replace(
                temporary_final_path,
                final_bif_path,
            )

            log(
                f"--> Generated BIF: {final_bif_path} "
                f"({os.path.getsize(final_bif_path)} bytes)"
            )

            return True

    except subprocess.TimeoutExpired:
        log(
            f"<!> biftool timed out after {BIFTOOL_TIMEOUT_SECONDS}s "
            f"for {os.path.basename(video_path)}"
        )

    except (OSError, ValueError, shutil.Error) as ex:
        log(
            f"<!> BIF generation error for {os.path.basename(video_path)}: "
            f"{type(ex).__name__}: {ex}"
        )

    return False
