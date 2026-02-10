from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import time
import threading

# ============================================================================
# WEBROOT SERVER — CONTRACT
# ----------------------------------------------------------------------------
# This service:
# - Serves static UI, proxies, keyframes, and PIPELINE FILES (read-only)
# - Exposes a semantic search API backed by Milvus Lite
# - NEVER writes to the catalog
# - May be restarted independently of Stage B / C
#
# Additional contract discipline:
# - No inference from absence: /api/health surfaces explicit state and stats.
# - Snapshot-only: Milvus must open the snapshot DB path, never the catalog DB.
# - Snapshot refresh attempts are explicitly reported with reason codes.
# ============================================================================

READ_ONLY_API = True

# Milvus lifecycle policy
MILVUS_LAZY_INIT = True
MILVUS_IDLE_CLOSE_S = 60.0
MILVUS_IDLE_POLL_S = 2.0

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
WEBROOT = Path("/workspace/webroot")
UI_DIR = WEBROOT / "ui"
INDEX = WEBROOT / "index.json"

# !!! DO NOT REMOVE !!!
# Pipeline documents are intentionally readable over HTTP
PIPELINE_DIR = Path("/workspace/pipeline")

# Milvus catalog + snapshot
MILVUS_DB_CATALOG = "/workspace/catalog/milvus/moments.db"
WEBROOT_MILVUS_DIR = WEBROOT / "milvus"
MILVUS_DB_SNAPSHOT = str(WEBROOT_MILVUS_DIR / "moments.snapshot.db")
MILVUS_COLL = "moments_asr_v1"

# ----------------------------------------------------------------------------
# Embedder (E5 small v2, ONNX, CPU)
# ----------------------------------------------------------------------------
EMBED_QUERY_PREFIX = "query: "
ONNX_MODEL_PATH = "/workspace/models/onnx/e5-small-v2/model.onnx"
TOKENIZER_PATH = "/workspace/models/onnx/e5-small-v2/tokenizer.json"
MAX_LEN = 256
EMBED_NORMALIZE = True

# ----------------------------------------------------------------------------
# Init
# ----------------------------------------------------------------------------
PROCESS_START_T = time.time()

app = FastAPI(title="Media Webroot Server", version="1.0")

# ----------------------------------------------------------------------------
# Static mounts (EXPLICIT CONTRACT)
# ----------------------------------------------------------------------------
app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
app.mount("/proxies", StaticFiles(directory=str(WEBROOT / "proxies")), name="proxies")
app.mount("/keyframes", StaticFiles(directory=str(WEBROOT / "keyframes")), name="keyframes")

# !!! DO NOT REMOVE !!!
# Required for URLs like:
#   /pipeline/MASTER.md
#   /pipeline/*.md
app.mount("/pipeline", StaticFiles(directory=str(PIPELINE_DIR)), name="pipeline")

# ----------------------------------------------------------------------------
# Global state
# ----------------------------------------------------------------------------
_index_map = None
_index_lock = threading.Lock()

_embed_ready = False
_embed_lock = threading.Lock()
_np = None
_tok = None
_sess = None

_milvus_ready = False
_milvus_lock = threading.Lock()
_milvus_client = None
_milvus_error = None  # str or None
_milvus_last_used_t = 0.0

_milvus_backend_pid = None

_idle_thread_started = False
_idle_thread_lock = threading.Lock()

_snapshot_lock = threading.Lock()

# Snapshot refresh reporting (no inference)
_snapshot_status = {
    "startup_attempt": None,   # first attempt recorded after boot
    "last_attempt": None,      # most recent attempt
}

# ----------------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------------
def _safe_stat(path: Path):
    try:
        st = path.stat()
        return {
            "exists": True,
            "size": st.st_size,
            "mtime_epoch": st.st_mtime,
        }
    except FileNotFoundError:
        return {"exists": False}


def _utc_now_str():
    # Keep simple, no datetime dependency needed.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env_policy_flags():
    keys = [
        "MEDIA_WEBROOT_READ_ONLY",
        "MEDIA_WEBROOT_SNAPSHOT_ONLY",
        "MEDIA_WEBROOT_NEVER_OPEN_CATALOG_DB",
    ]
    return {k: os.environ.get(k) for k in keys}


def _env_provenance():
    return {
        "run_id": os.environ.get("RUN_ID"),
        "ts_utc": os.environ.get("TS_UTC"),
        "app_sha256": os.environ.get("APP_SHA256"),
        "app_file": os.environ.get("APP_FILE"),
        "python": os.environ.get("PY"),
    }


def _health_milvus_state():
    if _milvus_ready:
        return "ready"

    if _milvus_error:
        if isinstance(_milvus_error, str) and _milvus_error.startswith("closed:"):
            if "idle" in _milvus_error:
                return "closed_idle"
            return "closed"
        return "error"

    catalog_exists = Path(MILVUS_DB_CATALOG).exists()
    snapshot_exists = Path(MILVUS_DB_SNAPSHOT).exists()
    if not catalog_exists:
        return "no_catalog"
    if not snapshot_exists:
        return "no_snapshot"
    return "lazy_not_initialized"


def _record_snapshot_attempt(attempt: dict):
    # attempt must be JSON-serializable
    global _snapshot_status
    with _snapshot_lock:
        if _snapshot_status["startup_attempt"] is None:
            _snapshot_status["startup_attempt"] = attempt
        _snapshot_status["last_attempt"] = attempt


def _get_snapshot_status():
    with _snapshot_lock:
        # return shallow copies
        return {
            "startup_attempt": _snapshot_status["startup_attempt"],
            "last_attempt": _snapshot_status["last_attempt"],
        }


# ----------------------------------------------------------------------------
# Index loading (media_id → metadata)
# ----------------------------------------------------------------------------
def _load_index_map():
    global _index_map
    with _index_lock:
        if _index_map is not None:
            return _index_map
        if not INDEX.exists():
            _index_map = {}
            return _index_map
        obj = json.loads(INDEX.read_text("utf-8"))
        _index_map = {
            r["media_id"]: r
            for r in obj.get("records", [])
            if r.get("media_id")
        }
        return _index_map


# ----------------------------------------------------------------------------
# Embedder
# ----------------------------------------------------------------------------
def _init_embedder():
    global _embed_ready, _np, _tok, _sess
    with _embed_lock:
        if _embed_ready:
            return
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        _np = np
        _tok = Tokenizer.from_file(TOKENIZER_PATH)

        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1

        _sess = ort.InferenceSession(
            ONNX_MODEL_PATH,
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        _embed_ready = True


def _embed_query(text: str):
    if not _embed_ready:
        _init_embedder()

    q = EMBED_QUERY_PREFIX + text.strip()
    enc = _tok.encode(q)

    ids = enc.ids[:MAX_LEN]
    attn = [1] * len(ids)

    if len(ids) < MAX_LEN:
        pad = MAX_LEN - len(ids)
        ids += [0] * pad
        attn += [0] * pad

    input_ids = _np.array([ids], dtype=_np.int64)
    attention_mask = _np.array([attn], dtype=_np.int64)
    token_type_ids = _np.zeros_like(input_ids)

    out = _sess.run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )

    last_hidden = out[0]
    mask = attention_mask.astype("float32")[..., None]
    pooled = (last_hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(1e-9)
    vec = pooled[0].astype("float32")

    if EMBED_NORMALIZE:
        n = _np.linalg.norm(vec)
        if n > 1e-12:
            vec /= n

    return vec


# ----------------------------------------------------------------------------
# Milvus snapshot + lifecycle
# ----------------------------------------------------------------------------
def _refresh_snapshot_if_needed(force=False):
    t0 = time.time()
    src = Path(MILVUS_DB_CATALOG)
    dst = Path(MILVUS_DB_SNAPSHOT)

    attempt = {
        "ts_utc": _utc_now_str(),
        "force": bool(force),
        "reason_code": None,
        "duration_ms": None,
        "src": str(src),
        "dst": str(dst),
        "src_stat_before": _safe_stat(src),
        "dst_stat_before": _safe_stat(dst),
        "dst_stat_after": None,
        "tmp_path": None,
        "error": None,
    }

    try:
        if not src.exists():
            attempt["reason_code"] = "no_catalog"
            return False

        try:
            WEBROOT_MILVUS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            s = repr(e)
            if len(s) > 800:
                s = s[:800] + "...(truncated)"
            attempt["reason_code"] = "mkdir_failed"
            attempt["error"] = s
            return False

        src_st = attempt["src_stat_before"]
        dst_st = attempt["dst_stat_before"]

        if (
            not force
            and dst_st.get("exists")
            and dst_st.get("mtime_epoch") is not None
            and src_st.get("mtime_epoch") is not None
            and dst_st["mtime_epoch"] >= src_st["mtime_epoch"]
            and dst_st.get("size", 0) > 0
        ):
            attempt["reason_code"] = "up_to_date"
            attempt["dst_stat_after"] = dst_st
            return True

        tmp = WEBROOT_MILVUS_DIR / f".snapshot.tmp.{int(time.time()*1000)}"
        attempt["tmp_path"] = str(tmp)

        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        except Exception as e:
            s = repr(e)
            if len(s) > 800:
                s = s[:800] + "...(truncated)"
            attempt["reason_code"] = "copy_failed"
            attempt["error"] = s
            # best-effort cleanup
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False

        attempt["reason_code"] = "refreshed"
        attempt["dst_stat_after"] = _safe_stat(dst)
        return True

    except Exception as e:
        s = repr(e)
        if len(s) > 800:
            s = s[:800] + "...(truncated)"
        attempt["reason_code"] = "unknown_error"
        attempt["error"] = s
        return False

    finally:
        attempt["duration_ms"] = int(round((time.time() - t0) * 1000))
        _record_snapshot_attempt(attempt)


def _detect_milvus_backend_pid():
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(os.getpid()), "milvus"],
            stderr=subprocess.DEVNULL,
        )
        pids = [int(x) for x in out.decode().splitlines() if x.isdigit()]
        return max(pids) if pids else None
    except Exception:
        return None


def _close_milvus(reason=""):
    global _milvus_ready, _milvus_client, _milvus_backend_pid, _milvus_error
    with _milvus_lock:
        try:
            if _milvus_client:
                _milvus_client.release_collection(MILVUS_COLL)
                _milvus_client.close()
        except Exception:
            pass

        if _milvus_backend_pid:
            try:
                os.kill(_milvus_backend_pid, signal.SIGTERM)
            except Exception:
                pass

        _milvus_client = None
        _milvus_backend_pid = None
        _milvus_ready = False
        if reason:
            _milvus_error = f"closed: {reason}"


def _ensure_milvus_ready():
    global _milvus_ready, _milvus_client, _milvus_last_used_t, _milvus_error

    if _milvus_ready:
        _milvus_last_used_t = time.time()
        return True

    # Ensure snapshot exists/up-to-date (best-effort). This does not open the DB.
    if not _refresh_snapshot_if_needed(force=False):
        _milvus_error = "error: snapshot_refresh_failed_or_no_catalog"
        return False

    from pymilvus import MilvusClient

    try:
        client = MilvusClient(MILVUS_DB_SNAPSHOT)
        client.load_collection(MILVUS_COLL)
        _milvus_client = client
        _milvus_backend_pid = _detect_milvus_backend_pid()
        _milvus_ready = True
        _milvus_last_used_t = time.time()
        _milvus_error = None
        return True
    except Exception as e:
        s = repr(e)
        if len(s) > 1000:
            s = s[:1000] + "...(truncated)"
        _milvus_error = f"error: {s}"
        return False


def _idle_closer():
    while True:
        time.sleep(MILVUS_IDLE_POLL_S)
        if _milvus_ready and time.time() - _milvus_last_used_t > MILVUS_IDLE_CLOSE_S:
            _close_milvus(f"idle>{MILVUS_IDLE_CLOSE_S}s")


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    _load_index_map()
    _init_embedder()
    _refresh_snapshot_if_needed(force=False)

    global _idle_thread_started
    with _idle_thread_lock:
        if not _idle_thread_started:
            threading.Thread(target=_idle_closer, daemon=True).start()
            _idle_thread_started = True


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get("/")
def root():
    return RedirectResponse("/ui/")


@app.get("/index.json")
def index():
    return FileResponse(INDEX)


@app.get("/api/health")
def health():
    catalog_path = Path(MILVUS_DB_CATALOG)
    snapshot_path = Path(MILVUS_DB_SNAPSHOT)

    return {
        "ok": True,
        "service": {
            "name": "media_webroot",
            "uptime_s": round(time.time() - PROCESS_START_T, 2),
        },
        "policy": {
            "read_only_api": READ_ONLY_API,
            "milvus_lazy_init": MILVUS_LAZY_INIT,
            "milvus_idle_close_s": MILVUS_IDLE_CLOSE_S,
            "milvus_idle_poll_s": MILVUS_IDLE_POLL_S,
            "policy_flags": _env_policy_flags(),
        },
        "provenance": _env_provenance(),
        "paths": {
            "webroot": str(WEBROOT),
            "ui_dir": str(UI_DIR),
            "index_json": str(INDEX),
            "index_stat": _safe_stat(INDEX),
            "pipeline_dir": str(PIPELINE_DIR),
            "catalog_db": str(catalog_path),
            "catalog_db_stat": _safe_stat(catalog_path),
            "snapshot_db": str(snapshot_path),
            "snapshot_db_stat": _safe_stat(snapshot_path),
            "active_db_path": MILVUS_DB_SNAPSHOT,
        },
        "snapshot_refresh": _get_snapshot_status(),
        "embedder": {
            "ready": _embed_ready,
            "model_id": "intfloat/e5-small-v2",
            "query_prefix": EMBED_QUERY_PREFIX,
            "pooling": "mean_attention",
            "normalize": EMBED_NORMALIZE,
            "onnx_model_path": ONNX_MODEL_PATH,
            "tokenizer_path": TOKENIZER_PATH,
            "max_len": MAX_LEN,
        },
        "milvus": {
            "state": _health_milvus_state(),
            "ready": _milvus_ready,
            "collection": MILVUS_COLL,
            "last_error": _milvus_error,
            "last_used_s_ago": None if not _milvus_last_used_t else round(time.time() - _milvus_last_used_t, 3),
            "backend_pid": _milvus_backend_pid,
        },
    }


@app.get("/api/semantic")
def semantic(
    q: str = Query(..., min_length=1, max_length=400),
    k: int = Query(20, ge=1, le=100),
):
    if not _ensure_milvus_ready():
        return JSONResponse({"error": "milvus_not_ready", "last_error": _milvus_error}, 503)

    vec = _embed_query(q)

    res = _milvus_client.search(
        collection_name=MILVUS_COLL,
        data=[vec.tolist()],
        anns_field="vector",
        limit=k,
        output_fields=["media_id", "start_s", "end_s", "text"],
    )

    index_map = _load_index_map()
    hits = []

    for hit in res[0]:
        ent = hit["entity"]
        rec = index_map.get(ent.get("media_id"), {})

        hits.append(
            {
                "score": hit["distance"],
                "media_id": ent.get("media_id"),
                "start_s": ent.get("start_s"),
                "end_s": ent.get("end_s"),
                "text": ent.get("text"),
                "source_name": rec.get("source_name"),
            }
        )

    return {
        "query": q,
        "count": len(hits),
        "results": hits,
    }