from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


CURRENT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = CURRENT_DIR.parent
RUNTIME_ROOT = CURRENT_DIR / "runtime"
DEFAULT_HOST = os.environ.get("PS_AGENT_SAM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PS_AGENT_SAM_PORT", "17861"))
DEFAULT_MODEL_PATH = CURRENT_DIR / "models" / "sam2" / "sam2.1_hiera_base_plus.pt"
MODEL_PATH = Path(os.environ.get("PS_AGENT_SAM_MODEL_PATH", str(DEFAULT_MODEL_PATH))).expanduser()
MODEL_CONFIG = os.environ.get("PS_AGENT_SAM_CONFIG", "configs/sam2.1/sam2.1_hiera_b+.yaml")
PID_PATH = RUNTIME_ROOT / "sam-worker.pid"

PREDICTOR = None
PREDICTOR_DEVICE = None
PREDICTOR_LOADED_AT = None
PREDICTOR_LOCK = threading.Lock()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


def api_error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON request body must be an object.")
    return data


def dependency_status(module_name: str) -> dict[str, Any]:
    return {"module": module_name, "available": importlib.util.find_spec(module_name) is not None}


def torch_status() -> dict[str, Any]:
    status = dependency_status("torch")
    if not status["available"]:
        return status
    try:
        import torch

        status.update(
            {
                "version": getattr(torch, "__version__", None),
                "cuda_available": torch.cuda.is_available(),
                "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
                "cuda_memory_total": torch.cuda.get_device_properties(0).total_memory if torch.cuda.is_available() else None,
            }
        )
    except Exception as exc:
        status.update({"available": False, "error": str(exc)})
    return status


def health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "worker": "sam2.1",
        "process": {
            "pid": os.getpid(),
            "started_at": STARTED_AT_ISO,
            "uptime_seconds": round(time.time() - STARTED_AT_TS, 3),
        },
        "model": {
            "path": str(MODEL_PATH),
            "exists": MODEL_PATH.is_file(),
            "size_bytes": MODEL_PATH.stat().st_size if MODEL_PATH.is_file() else None,
            "config": MODEL_CONFIG,
        },
        "dependencies": {
            "python": sys.version,
            "pillow": dependency_status("PIL"),
            "numpy": dependency_status("numpy"),
            "torch": torch_status(),
            "sam2": dependency_status("sam2"),
        },
        "predictor": {
            "loaded": PREDICTOR is not None,
            "device": PREDICTOR_DEVICE,
            "loaded_at": PREDICTOR_LOADED_AT,
        },
    }


def load_predictor() -> tuple[Any, Any, str]:
    global PREDICTOR, PREDICTOR_DEVICE, PREDICTOR_LOADED_AT
    if PREDICTOR is not None:
        return PREDICTOR, __import__("torch"), str(PREDICTOR_DEVICE)

    if not MODEL_PATH.is_file():
        raise RuntimeError(f"Missing SAM 2.1 Base+ checkpoint: {MODEL_PATH}")

    try:
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
    except ImportError as exc:
        raise RuntimeError(
            "SAM worker dependencies are missing. Install torch and sam2 inside D:\\Photo_sontrol\\.venv-sam."
        ) from exc

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build_sam2(MODEL_CONFIG, str(MODEL_PATH), device=device)
    PREDICTOR = SAM2ImagePredictor(model)
    PREDICTOR_DEVICE = device
    PREDICTOR_LOADED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return PREDICTOR, torch, device


def normalize_prompt(prompt: Any) -> dict[str, Any]:
    if not isinstance(prompt, dict):
        raise ValueError("prompt must be an object")
    box = prompt.get("box")
    point_coords = prompt.get("point_coords")
    point_labels = prompt.get("point_labels")
    if box is None and not point_coords:
        raise ValueError("prompt.box or prompt.point_coords is required")
    if box is not None:
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError("prompt.box must be [left, top, right, bottom]")
        box = [float(item) for item in box]
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("prompt.box must have positive width and height")
    if point_coords is not None:
        if not isinstance(point_coords, list) or not isinstance(point_labels, list):
            raise ValueError("prompt.point_coords and prompt.point_labels must be arrays")
        if len(point_coords) != len(point_labels):
            raise ValueError("prompt.point_coords and prompt.point_labels must have the same length")
        parsed_points = []
        parsed_labels = []
        for index, point in enumerate(point_coords):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"prompt.point_coords[{index}] must be [x, y]")
            parsed_points.append([float(point[0]), float(point[1])])
            parsed_labels.append(1 if int(point_labels[index]) > 0 else 0)
        point_coords = parsed_points
        point_labels = parsed_labels
    return {"box": box, "point_coords": point_coords, "point_labels": point_labels}


def run_prediction(body: dict[str, Any]) -> dict[str, Any]:
    image_path = Path(str(body.get("image_path") or "")).expanduser()
    output_mask_path = Path(str(body.get("output_mask_path") or "")).expanduser()
    if not image_path.is_file():
        return api_error("image_not_found", f"Image does not exist: {image_path}")
    if not str(output_mask_path):
        return api_error("output_mask_path_missing", "output_mask_path is required")
    output_mask_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        prompt = normalize_prompt(body.get("prompt"))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_sam_prompt", str(exc))

    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        return api_error(
            "sam_dependency_missing",
            "Pillow and numpy are required inside the SAM worker environment.",
            {"missing": str(exc)},
        )

    try:
        with PREDICTOR_LOCK:
            predictor, torch, device = load_predictor()
            with Image.open(image_path) as image_file:
                image = np.array(image_file.convert("RGB"))

            box = np.array(prompt["box"], dtype=np.float32) if prompt.get("box") is not None else None
            point_coords = (
                np.array(prompt["point_coords"], dtype=np.float32)
                if prompt.get("point_coords") is not None
                else None
            )
            point_labels = (
                np.array(prompt["point_labels"], dtype=np.int32)
                if prompt.get("point_labels") is not None
                else None
            )

            def predict_once():
                predictor.set_image(image)
                return predictor.predict(
                    point_coords=point_coords,
                    point_labels=point_labels,
                    box=box,
                    multimask_output=body.get("multimask_output", True) is not False,
                )

            if device == "cuda":
                with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
                    masks, scores, _ = predict_once()
            else:
                with torch.inference_mode():
                    masks, scores, _ = predict_once()

            best_index = int(np.argmax(scores))
            best_mask = masks[best_index].astype("uint8") * 255
            Image.fromarray(best_mask, mode="L").save(output_mask_path)

            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "score": float(scores[best_index]),
                "score_candidates": [float(item) for item in scores.tolist()],
                "best_index": best_index,
                "device": device,
                "mask_path": str(output_mask_path),
                "mask_size": {"width": int(best_mask.shape[1]), "height": int(best_mask.shape[0])},
            }
    except RuntimeError as exc:
        message = str(exc)
        details: dict[str, Any] = {"model_path": str(MODEL_PATH), "config": MODEL_CONFIG}
        try:
            import torch

            if "out of memory" in message.lower() and torch.cuda.is_available():
                torch.cuda.empty_cache()
                details["cuda_cache_cleared"] = True
        except Exception:
            pass
        code = "sam_cuda_oom" if "out of memory" in message.lower() else "sam_predict_failed"
        return api_error(code, message, details)
    except Exception as exc:
        return api_error("sam_predict_failed", str(exc), {"model_path": str(MODEL_PATH), "config": MODEL_CONFIG})


def client_is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    host = handler.client_address[0]
    return host in {"127.0.0.1", "::1", "localhost"}


class SamWorkerHandler(BaseHTTPRequestHandler):
    server_version = "PSUXPSAMWorker/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        try:
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))
        except Exception:
            pass

    def do_OPTIONS(self) -> None:
        json_response(self, 200, {"status": "ok"})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            json_response(self, 200, health_payload())
            return
        json_response(self, 404, api_error("not_found", f"Unknown endpoint: {path}"))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            body = parse_json_body(self)
        except Exception as exc:
            json_response(self, 400, api_error("invalid_json", str(exc)))
            return

        if path == "/predict":
            result = run_prediction(body)
            json_response(self, 200 if result.get("status") == "ok" else 500, result)
            return

        if path == "/shutdown":
            if not client_is_loopback(self):
                json_response(self, 403, api_error("shutdown_forbidden", "Shutdown is only allowed from loopback."))
                return
            json_response(self, 200, {"status": "ok", "message": "SAM worker shutdown requested."})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        json_response(self, 404, api_error("not_found", f"Unknown endpoint: {path}"))


def write_pid() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid() -> None:
    try:
        if PID_PATH.is_file():
            PID_PATH.unlink()
    except OSError:
        pass


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    write_pid()
    server = ThreadingHTTPServer((host, port), SamWorkerHandler)
    print(f"PS UXP Agent SAM worker listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        clear_pid()


STARTED_AT_TS = time.time()
STARTED_AT_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED_AT_TS))


if __name__ == "__main__":
    run_server()
