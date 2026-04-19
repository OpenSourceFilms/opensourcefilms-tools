# Phase 1 Status — Media Evidence Retrieval System

**Date:** 2026-04-19
**Status:** COMPLETE

---

## Pre-flight Fixes

### Fix 1: Dramatiq → RQ
- Replaced `dramatiq[redis]` with `rq==2.3.3` across all 5 ingest modules
- All `@dramatiq.actor` decorators removed; enqueue via `ingest_queue.enqueue(fn, arg)`
- Workers: `rq worker ingest`, `rq worker ingest_gpu`, `rq worker embed`
- RQ config: `workers/rq_config.py` — three queues: `ingest`, `ingest_gpu`, `embed`

### Fix 2: PostgreSQL Persistence
- Postgres running at `/pgdata` (ephemeral container overlay — survives process restart but not pod reimage)
- Option B strategy active: cron dump every 15 min to `/workspace/.pod-persist/postgres/media_evidence.sql.gz`
- Restore on pod start via `scripts/pg_restore_on_start.sh`

### Fix 3: Model Installation
| Model | Path | Status |
|-------|------|--------|
| Qwen3-Embedding-8B (text, 4096d) | `/workspace/weights/qwen3/Qwen3-Embedding-8B` | ✓ Loaded |
| Qwen3-VL-Embedding-2B (visual, 2048d) | `/workspace/weights/qwen3/Qwen3-VL-Embedding-2B` | ✓ Loaded |
| faster-whisper large-v3 (ASR) | HuggingFace cache | ✓ Working |
| PaddleOCR 3.4.1 | system Python | ✗ Backend incompatible (see OCR status) |

---

## Embedding Models

| Surface | Collection | Model | Dim | Notes |
|---------|-----------|-------|-----|-------|
| Visual keyframe | `visual_keyframe_qwen3vl` | Qwen3-VL-Embedding-2B | 2048 | ✓ |
| Visual shot | `visual_shot_qwen3vl` | Qwen3-VL-Embedding-2B | 2048 | mean pool over keyframes |
| Text sentence | `text_sentence_qwen3` | Qwen3-Embedding-8B | 4096 | last-token pooling |
| Text turn | `text_turn_qwen3` | Qwen3-Embedding-8B | 4096 | last-token pooling |
| OCR span | `ocr_span_qwen3` | Qwen3-Embedding-8B | 4096 | currently empty (see OCR status) |

**Embed venv:** `/workspace/venvs/qwen3_embed_clean` (Python 3.12, torch 2.8.0+cu128, transformers 5.5.4, sentence-transformers 5.4.1)

---

## Ingest Chain (verified end-to-end)

Asset: `test_clip.mp4` (90s, 1 shot, 2 keyframes)
Asset ID: `13b30e6f-80d4-49d7-a93c-44c62e17dbf2`
Project: `test-project-001` (`5a6cd11d-5490-4072-9fb9-bee4f2752e3d`)

| Stage | Status | Notes |
|-------|--------|-------|
| probe | ✓ | ffprobe metadata extraction |
| segment | ✓ | PySceneDetect (TransNetV2 weights unavailable — TF required for conversion) |
| extract_keyframes | ✓ | 2 keyframes at pts 15.0 and 45.0 |
| run_asr | ✓ | faster-whisper large-v3, 3 turns: "Thank you." ×3 |
| run_ocr | ✓ (no-op) | PaddleOCR init fails — gracefully skipped (see OCR status) |
| embed_asset | ✓ | Qwen3-VL-2B + Qwen3-Embedding-8B, 2m04s |

Final state: `ingest_state=complete`, `asr_done=true`, `ocr_done=true`

**Qdrant point counts:**
- `visual_keyframe_qwen3vl`: 2 pts
- `visual_shot_qwen3vl`: 1 pt
- `text_sentence_qwen3`: 4 pts
- `text_turn_qwen3`: 3 pts
- `ocr_span_qwen3`: 0 pts (OCR skipped)

---

## Search API

### Root cause of initial empty results
`embed.py` wrote text payloads (`text_sentence_qwen3`, `text_turn_qwen3`) without a `shot_id` field.
`_qdrant_search` in `search.py` falls back to the raw Qdrant point UUID when `shot_id` is absent —
this UUID never matches any `Shot.id` in Postgres, so `_build_results` returns nothing.

**Fix:** Added `shot_id` to all text/sentence payloads in `embed.py`. Patched existing points
via `qdrant_client.set_payload`.

### Visual cross-modal search (Phase 2)
Visual collections are 2048d (Qwen3-VL-2B output). Query embeddings are 4096d (Qwen3-Embedding-8B).
Cross-modal text→image search requires a shared embedding space — deferred to Phase 2.
Visual `surfaces_hit` reflects keyframe attachment to shot (correct), not a Qdrant hit.

### Validation queries (2026-04-19, asset `13b30e6f`)

All queries return non-empty results. Uniform scores (0.1124) expected: test clip has 1 shot,
transcript is "Thank you." repeated — no semantic differentiation between queries.

| Query | Results | Score | Surfaces |
|-------|---------|-------|----------|
| "camera operator adjusting focus" | 1 | 0.1124 | visual, text |
| "we need to do another take" | 1 | 0.1124 | visual, text |
| "interview talking to camera" | 1 | 0.1124 | visual, text |
| "thank you speech applause" | 1 | 0.1124 | visual, text |
| "outdoor location shoot" | 1 | 0.1124 | visual, text |

---

## OCR Status

PaddleOCR 3.4.1 is installed but incompatible with the installed paddle backend:
```
AttributeError: 'paddle.base.libpaddle.AnalysisConfig' object has no attribute 'set_optimization_level'
```
The `_get_paddle()` function in `ingest/ocr.py` catches this and logs a WARNING, producing 0 OCR spans.
The ingest chain continues normally — `ocr_done` is set to `true` after the graceful skip.

**Remediation options:**
- Downgrade PaddleOCR to 2.8.x (last version with old API)
- Switch to EasyOCR (`pip install easyocr`)
- Use Qwen2-VL ad-hoc for OCR-heavy frames (available via transformers)

---

## Worker Scripts

| Script | Queue | Venv |
|--------|-------|------|
| `scripts/start_worker_ingest.sh` | `ingest`, `ingest_gpu` | `/workspace/.pod-persist/venvs/pipeline-v2` |
| `scripts/start_worker_embed.sh` | `embed` | `/workspace/venvs/qwen3_embed_clean` |

---

## Known Limitations / Phase 2 Targets

1. **Visual cross-modal search** — needs a text encoder that produces 2048d vectors, or a projection layer
2. **TransNetV2** — using PySceneDetect fallback; TF SavedModel needs conversion to PyTorch `.pth`
3. **OCR** — currently a no-op; EasyOCR would be a straightforward drop-in
4. **Single test asset** — search semantics need a multi-shot, multi-topic corpus to validate ranking
5. **workers/__init__.py** — eagerly imported all ingest modules; now removed to support split venv workers
