#!/usr/bin/env python3
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract

LABELS = ["nsfl", "nsfw", "sfw"]
MATURE_LABELS = ["anime", "hentai", "neutral", "pornography", "sensual"]
MATURE_UNSAFE_LABELS = ("hentai", "pornography")
IMAGE_SIZE = 224
DHASH_SIZE = 8
DHASH_DUP_THRESHOLD = 0
COLOR_GRID = 4
COLOR_DUP_THRESHOLD = 2 * COLOR_GRID * COLOR_GRID
TILE_ASPECT_THRESHOLD = 3
TILE_AREA_THRESHOLD = 2_560_000
TARGET_TILE_DIM = 1400
MAX_TILES_PER_AXIS = 16
MAX_VIEWS = 256
DEFAULT_THREADS = 4
CONTAINER_FORMATS = ("office", "office-legacy", "ogg")


def _dhash(image, hash_size=DHASH_SIZE):
    small = image.convert("L").resize((hash_size + 1, hash_size))
    pixels = small.tobytes()
    value = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            value = (value << 1) | (1 if pixels[offset + col] < pixels[offset + col + 1] else 0)
    return value


def _hamming(a, b):
    return bin(a ^ b).count("1")


def _color_signature(image, grid=COLOR_GRID):
    small = image.convert("RGB").resize((grid, grid))
    return small.tobytes()


def _color_distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def _dedupe(frames):
    kept = []
    signatures = []
    for frame in frames:
        try:
            h = _dhash(frame.image)
            c = _color_signature(frame.image)
        except Exception:
            kept.append(frame)
            continue
        is_dup = False
        for eh, ec in signatures:
            if _hamming(h, eh) <= DHASH_DUP_THRESHOLD and _color_distance(c, ec) <= COLOR_DUP_THRESHOLD:
                is_dup = True
                break
        if is_dup:
            continue
        signatures.append((h, c))
        kept.append(frame)
    return kept


def _tiles_for(image, max_tiles):
    w, h = image.size
    if w <= 0 or h <= 0 or max_tiles <= 1:
        return [image]

    aspect = max(w, h) / max(1, min(w, h))
    if aspect <= TILE_ASPECT_THRESHOLD and w * h <= TILE_AREA_THRESHOLD and max(w, h) <= TARGET_TILE_DIM:
        return [image]

    cols = max(1, min(MAX_TILES_PER_AXIS, -(-w // TARGET_TILE_DIM)))
    rows = max(1, min(MAX_TILES_PER_AXIS, -(-h // TARGET_TILE_DIM)))
    while cols * rows + 1 > max_tiles and (cols > 1 or rows > 1):
        if cols >= rows and cols > 1:
            cols -= 1
        elif rows > 1:
            rows -= 1
        else:
            break

    tiles = []
    for row in range(rows):
        for col in range(cols):
            box = (col * w // cols, row * h // rows, (col + 1) * w // cols, (row + 1) * h // rows)
            if box[2] > box[0] and box[3] > box[1]:
                tiles.append(image.crop(box))

    if not tiles:
        tiles = [image]
    elif len(tiles) > 1:
        tiles.append(image)
    return tiles[:max_tiles]


def _softmax(np, values):
    shifted = np.exp(values - np.max(values))
    return shifted / shifted.sum()


def _score_image(session, mature, np, image):
    resized = image.convert("RGB").resize((IMAGE_SIZE, IMAGE_SIZE), 2)
    array = np.asarray(resized, dtype=np.float32).transpose(2, 0, 1)[np.newaxis, :, :, :]
    scores = session.run(None, {"image": array})[0][0]
    result = {label: float(score) for label, score in zip(LABELS, scores)}

    if mature is not None:
        session2, input_name = mature
        raw = session2.run(None, {input_name: array / 127.5 - 1.0})[0][0]
        probabilities = _softmax(np, raw)
        detail = {label: float(p) for label, p in zip(MATURE_LABELS, probabilities)}
        unsafe = sum(detail[label] for label in MATURE_UNSAFE_LABELS)
        result["mature"] = detail
        if 1.0 - unsafe < result["sfw"]:
            result["sfw"] = 1.0 - unsafe
    return result


def main():
    argv = sys.argv[1:]
    inventory_only = "--inventory" in argv
    argv = [a for a in argv if a != "--inventory"]

    threads = DEFAULT_THREADS
    if "--threads" in argv:
        idx = argv.index("--threads")
        try:
            threads = max(1, int(argv[idx + 1]))
        except (IndexError, ValueError):
            print("usage: classify.py [--threads N] [--mature-model M] [--budget-seconds N] <model.onnx> <path>", file=sys.stderr)
            return 1
        argv = argv[:idx] + argv[idx + 2:]

    mature_model_path = None
    if "--mature-model" in argv:
        idx = argv.index("--mature-model")
        try:
            mature_model_path = argv[idx + 1]
        except IndexError:
            print("usage: classify.py [--mature-model M] [--budget-seconds N] <model.onnx> <path>", file=sys.stderr)
            return 1
        argv = argv[:idx] + argv[idx + 2:]

    budget_seconds = None
    if "--budget-seconds" in argv:
        idx = argv.index("--budget-seconds")
        try:
            budget_seconds = float(argv[idx + 1])
        except (IndexError, ValueError):
            print("usage: classify.py [--budget-seconds N] [--inventory] <model.onnx> <path>", file=sys.stderr)
            return 1
        argv = argv[:idx] + argv[idx + 2:]

    if inventory_only:
        if len(argv) != 1:
            print("usage: classify.py --inventory <path>", file=sys.stderr)
            return 1
        model_path, path = None, argv[0]
    else:
        if len(argv) != 2:
            print("usage: classify.py <model.onnx> <image_path>", file=sys.stderr)
            return 1
        model_path, path = argv

    budget = extract.Budget(max_seconds=budget_seconds) if budget_seconds is not None else extract.Budget()

    try:
        extraction = extract.Extraction(path, budget=budget)
    except extract.UnsupportedFormat as exc:
        print(json.dumps({"error": "unsupported-format", "message": str(exc), "flags": {}}), file=sys.stderr)
        return 3

    try:
        frames = list(extraction.iter_frames())
    except extract.BudgetExceeded as exc:
        print(json.dumps({"error": "budget-exceeded", "message": str(exc), "flags": extraction.flags}), file=sys.stderr)
        return 2
    except extract.UnsupportedFormat as exc:
        print(json.dumps({"error": "unsupported-format", "message": str(exc), "flags": extraction.flags}), file=sys.stderr)
        return 3

    if not frames:
        if extraction.format in CONTAINER_FORMATS:
            empty = {label: 0.0 for label in LABELS}
            empty["sfw"] = 1.0
            empty["format"] = extraction.format
            empty["frames_decoded"] = 0
            empty["frames_scanned"] = 0
            empty["worst_view"] = None
            empty["flags"] = dict(extraction.flags, no_scannable_content=True)
            print(json.dumps(empty))
            return 0
        print("classify.py: no frames could be decoded", file=sys.stderr)
        return 1

    kept = _dedupe(frames)

    if inventory_only:
        result = {
            "format": extraction.format,
            "frames_decoded": len(frames),
            "frames_scanned": len(kept),
            "flags": extraction.flags,
        }
        print(json.dumps(result))
        return 0

    import numpy as np
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    session = onnxruntime.InferenceSession(
        model_path, sess_options=options, providers=["CPUExecutionProvider"]
    )

    mature = None
    if mature_model_path:
        mature_session = onnxruntime.InferenceSession(
            mature_model_path, sess_options=options, providers=["CPUExecutionProvider"]
        )
        mature = (mature_session, mature_session.get_inputs()[0].name)

    worst_view = None
    worst_score = None
    scanned = 0
    per_frame_views = max(1, MAX_VIEWS // max(1, len(kept)))
    for frame in kept:
        for tile_index, tile in enumerate(_tiles_for(frame.image, per_frame_views)):
            scanned += 1
            score = _score_image(session, mature, np, tile)
            view_name = frame.view if tile_index == 0 else f"{frame.view}:tile{tile_index}"
            if worst_score is None or score["sfw"] < worst_score["sfw"]:
                worst_score = score
                worst_view = view_name

    result = dict(worst_score)
    result["format"] = extraction.format
    result["frames_decoded"] = len(frames)
    result["frames_scanned"] = scanned
    result["worst_view"] = worst_view
    result["flags"] = extraction.flags
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"classify.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
