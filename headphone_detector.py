import os
import io
import base64
from pathlib import Path

from PIL import Image
import numpy as np

# Your exact absolute Windows path
_MODEL_PATH = Path(
    os.getenv("HEADPHONE_MODEL_PATH", r"C:\Users\abhishekdubey\PycharmProjects\Candidate_portal\models\best.pt")
)

# 0: Book, 1: Earphone, 2: Mobile_phone, 4: headset
TARGET_CLASSES = [0, 1, 2, 4]

# High thresholds for the new highly-accurate model
CLASS_CONF = {
    "book": 0.50,
    "earphone": 0.40,      # was 0.45
    "mobile_phone": 0.50,
    "headset": 0.40,       # was 0.45
}

GLOBAL_CONF_FLOOR = 0.40
_MODEL = None


def _is_forbidden_class(name):
    if not name:
        return False
    n = name.strip().lower().replace("-", "_").replace(" ", "_")
    return any(tok in n for tok in ("book", "earphone", "headset", "mobile_phone", "cell_phone"))


def _class_threshold(name):
    if not name:
        return 0.45
    n = name.strip().lower().replace("-", "_").replace(" ", "_")
    return CLASS_CONF.get(n, 0.45)


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from ultralytics import YOLO
    except Exception as e:
        print(f"[headphone_detector] ultralytics missing: {e}")
        return None

    if not _MODEL_PATH.is_file():
        print(f"❌ [headphone_detector] MODEL NOT FOUND AT: {_MODEL_PATH}")
        return None

    try:
        m = YOLO(str(_MODEL_PATH))
        names = list(m.names.values()) if hasattr(m, "names") else []
        print(f"✅ [headphone_detector] Loaded {_MODEL_PATH}")
        print(f"   Classes detected: {names}")
        _MODEL = m
        return m
    except Exception as e:
        print(f"❌ [headphone_detector] load error: {e}")
        return None


def _decode_frame(frame_b64):
    if "," in frame_b64:
        frame_b64 = frame_b64.split(",", 1)[1]
    raw = base64.b64decode(frame_b64, validate=False)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _parse_result(result, model):
    if result is None or result.boxes is None:
        return []
    names = model.names if hasattr(model, "names") else {}
    out = []
    for box in result.boxes:
        try:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cls_id = int(box.cls[0].item())
            c = float(box.conf[0].item())
            cls_name = (names.get(cls_id, f"class_{cls_id}") if isinstance(names, dict) else names[cls_id])
        except Exception:
            continue
        out.append({"class": cls_name, "confidence": c, "bbox": [float(x1), float(y1), float(x2), float(y2)]})
    return out


def _nms(dets, iou_threshold=0.40):
    if not dets: return dets
    dets = sorted(dets, key=lambda d: d["confidence"], reverse=True)
    keep = []
    for d in dets:
        x1, y1, x2, y2 = d["bbox"]
        ok = True
        for k in keep:
            kx1, ky1, kx2, ky2 = k["bbox"]
            ix1, iy1 = max(x1, kx1), max(y1, ky1)
            ix2, iy2 = min(x2, kx2), min(y2, ky2)
            iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
            inter = iw * ih
            a1 = max(0, x2 - x1) * max(0, y2 - y1)
            a2 = max(0, kx2 - kx1) * max(0, ky2 - ky1)
            u = a1 + a2 - inter
            if u > 0 and inter / u > iou_threshold:
                ok = False
                break
        if ok:
            keep.append(d)
    return keep


def _filter_by_class_threshold(dets):
    return [d for d in dets if d["confidence"] >= _class_threshold(d["class"]) and (not d["class"] or _is_forbidden_class(d["class"]))]


def is_available():
    return _load_model() is not None


def info():
    m = _load_model()
    names = list(m.names.values()) if (m and hasattr(m, "names")) else []
    return {
        "available": m is not None,
        "model_path": str(_MODEL_PATH),
        "class_names": names,
        "audio_classes_present": [n for n in names if _is_forbidden_class(n)],
        "class_thresholds": CLASS_CONF,
    }


def detect_from_base64(frame_b64, conf=None, imgsz=640, use_tiling=False):
    model = _load_model()
    if model is None: return []
    try:
        img = _decode_frame(frame_b64)
        arr = np.array(img)
    except Exception as e:
        print(f"[headphone_detector] decode error: {e}")
        return []

    floor = GLOBAL_CONF_FLOOR if conf is None else min(float(conf), GLOBAL_CONF_FLOOR)
    pooled = []
    try:
        r = model.predict(source=arr, imgsz=imgsz, conf=floor, iou=0.45, max_det=100, verbose=False, classes=TARGET_CLASSES)[0]
        pooled.extend(_parse_result(r, model))
    except Exception as e:
        print(f"[headphone_detector] inference err: {e}")

    merged = _nms(_filter_by_class_threshold(pooled))
    merged.sort(key=lambda d: d["confidence"], reverse=True)
    return merged


def detect_batch_from_base64(frames_b64, conf=None, imgsz=640, use_tiling=False):
    # Processes 1 by 1 to prevent memory/thread crashes in Flask
    model = _load_model()
    if model is None: return [[] for _ in frames_b64]
    if not frames_b64: return []

    floor = GLOBAL_CONF_FLOOR if conf is None else min(float(conf), GLOBAL_CONF_FLOOR)
    out = []
    for fb in frames_b64:
        try:
            img = _decode_frame(fb)
            arr = np.array(img)
            r = model.predict(source=arr, imgsz=imgsz, conf=floor, iou=0.45, max_det=100, verbose=False, classes=TARGET_CLASSES)[0]
            dets = _parse_result(r, model)
            dets = _nms(_filter_by_class_threshold(dets))
            dets.sort(key=lambda d: d["confidence"], reverse=True)
            out.append(dets)
        except Exception as e:
            print(f"[headphone_detector] batch frame err: {e}")
            out.append([])
    return out