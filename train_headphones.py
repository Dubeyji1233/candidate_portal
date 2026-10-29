"""
Local headphone detector training — completely free, runs on your PC.
Downloads the flexx/earphone-detection v4 dataset from Roboflow,
trains a YOLOv8n model, and copies best.pt to models/headphone_yolov8.pt
"""
import os
import shutil
import sys
from pathlib import Path

# ─── Your dataset (from the Roboflow snippet) ──────────────────────
ROBOFLOW_API_KEY   = os.getenv("ROBOFLOW_API_KEY", "GYN6Np9r1myPFB6B1N8Y")   # rotate after setup!
ROBOFLOW_WORKSPACE = "flexx"
ROBOFLOW_PROJECT   = "earphone-detection"
ROBOFLOW_VERSION   = 4
# ───────────────────────────────────────────────────────────────────

DATA_DIR   = Path("datasets/earphone-detection")
MODELS_DIR = Path("models")
MODELS_DIR.mkdir(exist_ok=True)


def download_dataset():
    if DATA_DIR.exists() and (DATA_DIR / "data.yaml").is_file():
        print(f"[SKIP] Dataset already at {DATA_DIR}")
        return
    try:
        from roboflow import Roboflow
    except ImportError:
        sys.exit("roboflow not installed. Run: pip install roboflow")
    rf = Roboflow(api_key=ROBOFLOW_API_KEY)
    project = rf.workspace(ROBOFLOW_WORKSPACE).project(ROBOFLOW_PROJECT)
    ds = project.version(ROBOFLOW_VERSION).download(
        "yolov8", location=str(DATA_DIR)
    )
    print(f"[OK] Dataset downloaded to {ds.location}")


def show_dataset_summary():
    import yaml
    with open(DATA_DIR / "data.yaml") as f:
        cfg = yaml.safe_load(f)
    names = cfg.get("names") or []
    print("\n──── Dataset summary ────")
    print(f"  Classes ({len(names)}): {names}")
    for split in ("train", "valid", "test"):
        p = DATA_DIR / split / "images"
        n = len(list(p.glob("*"))) if p.exists() else 0
        print(f"  {split:>5}: {n} images")
    print("─────────────────────────\n")
    if not names:
        sys.exit("data.yaml has no classes — dataset is broken.")
    total = sum(
        len(list((DATA_DIR / s / "images").glob("*")))
        for s in ("train", "valid", "test")
        if (DATA_DIR / s / "images").exists()
    )
    if total < 100:
        print(f"⚠️  Only {total} images total. Model will be weak. "
              f"Consider a bigger dataset from universe.roboflow.com.")


def train_model():
    from ultralytics import YOLO
    data_yaml = DATA_DIR / "data.yaml"
    if not data_yaml.is_file():
        sys.exit(f"data.yaml missing at {data_yaml}")

    print("[INFO] Training YOLOv8n — this typically takes 10–40 minutes on CPU.")
    model = YOLO("yolov8n.pt")
    model.train(
        data=str(data_yaml),
        epochs=40,
        imgsz=640,
        batch=8,
        project="runs/earphone",
        name="train",
        exist_ok=True,
    )


def copy_best():
    best = Path("runs/earphone/train/weights/best.pt")
    if not best.is_file():
        sys.exit(f"Training finished but best.pt is missing at {best}")
    dst = MODELS_DIR / "headphone_yolov8.pt"
    shutil.copy(best, dst)
    print(f"\n[OK] Model saved to {dst}")
    print("[NEXT] Restart your Flask app — the detector will load it automatically.")


if __name__ == "__main__":
    print("=" * 55)
    print("  Local headphone detector training (free, offline)")
    print("=" * 55)
    download_dataset()
    show_dataset_summary()
    train_model()
    copy_best()