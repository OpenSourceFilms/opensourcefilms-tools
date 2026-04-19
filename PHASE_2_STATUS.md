# Phase 2 Status — Entity Enrichment Layer

**Date:** 2026-04-19
**Status:** COMPLETE (pending live validation — enrichment stages ready to run)

---

## Schema Migration

Migration `0004_phase2_entity_layer` applied successfully.

| Table / Column | Change |
|---|---|
| `character` | New table (project_id, name, cluster_label, reference_count, confirmed) |
| `face_track` | New table (shot_id, asset_id, track_index, start_pts, end_pts, bbox_series JSONB, embedding ARRAY(Float) 512d, character_id FK, confidence, qdrant_point_id) |
| `object_track` | New table (shot_id, asset_id, track_index, label, confidence, start_pts, end_pts, bbox_series JSONB, crop_path) |
| `transcript_turn.character_id` | FK constraint added (column existed from Phase 1) |
| `asset.enrich_done` | Boolean flag added |

---

## New Ingest Stages

### Stage 6a — Face Detection (`ingest/faces.py`)

- Model: InsightFace `buffalo_l` (installed: v0.7.3)
- Runs on every keyframe per shot
- IoU-based greedy tracker across keyframes (ByteTrack was not installed — `lap` build failure)
- Per-track embedding: mean of per-frame 512d embeddings, L2-normalised
- Writes `FaceTrack` rows, upserts to `face_insightface` Qdrant collection (512d cosine)
- Gated by `settings.face_detection_enabled` (default: `true`)
- Increments `enrich_barrier:{asset_id}` Redis counter on completion

### Stage 6b — Object Detection (`ingest/objects.py`)

- Model: GroundingDINO SwinT-OGC
- **Weights not present at `/workspace/weights/groundingdino/` — stage skips gracefully**
- When weights are present: runs on representative keyframe per shot, writes `ObjectTrack` rows, saves 224×224 crops to `media-crops` MinIO bucket
- Default detection labels: `person . camera . microphone . monitor . clapperboard`
- Gated by `settings.object_detection_enabled` (default: `true`) and weights presence check
- Increments `enrich_barrier:{asset_id}` Redis counter on completion (even when skipping)

### Stage 6c — Shot Metadata (`ingest/shot_meta.py`)

- CPU-only; runs in the `ingest` worker
- Per-shot metrics:
  - `motion_score` — mean Farneback optical flow magnitude between consecutive keyframes
  - `mean_brightness`, `mean_contrast` — Y-channel stats across keyframes
  - `palette_hex` — k-means (k=5) in LAB colour space, hex strings sorted by cluster size
  - `shot_size_class` — heuristic from tallest face bbox height: ECU/CU/MCU/MS/LS/ELS/unknown
- Updates `Shot` columns directly (no Qdrant write)
- Increments `enrich_barrier:{asset_id}` Redis counter on completion

### Enrich Barrier

All three stages share a Redis counter `enrich_barrier:{asset_id}` (TTL: 1 hour).
When count reaches 3, `_finalize_enrichment` sets `ingest_state='complete'` and `enrich_done=true`.

### State Machine

```
pending → probing → segmenting → keyframing → transcribing → embedding → enriching → complete
                                                                                    ↑
                                              embed_asset sets 'enriching' and enqueues 6a/6b/6c
```

---

## Qdrant Collections

| Collection | Dimensions | Distance | Written by |
|---|---|---|---|
| `face_insightface` | 512 | Cosine | Stage 6a |
| `object_crop_qwen3vl` | 2048 | Cosine | Stage 6b (stub — requires GroundingDINO + embed worker) |

`face_insightface` is created on-demand by Stage 6a with payload indexes on: `asset_id`, `shot_id`, `character_id`, `project_id`, `deleted`.

---

## Character Clustering API

`POST /projects/{pid}/cluster_characters`
- DBSCAN on all face embeddings in the project (cosine distance, eps=0.4, min_samples=2)
- Creates `Character` rows per cluster (`cluster_0`, `cluster_1`, …)
- Sets `face_track.character_id` for all assigned tracks
- Noise tracks (label=-1) left unassigned
- Idempotent: re-uses existing `Character` rows by `cluster_label`

`GET /projects/{pid}/characters` — list all characters
`GET /projects/{pid}/characters/{cid}` — single character
`PATCH /projects/{pid}/characters/{cid}` — set `name`, `confirmed`

---

## Search API Filters (Phase 2 additions)

New fields on `SearchFilters`:

| Field | Type | Description |
|---|---|---|
| `character_ids` | `list[str]` | Restrict to shots where any of these characters appear |
| `object_labels` | `list[str]` | Restrict to shots with any of these object labels |
| `motion_score_min` | `float` | Lower bound on shot motion score |
| `motion_score_max` | `float` | Upper bound on shot motion score |
| `palette_hex` | `list[str]` | Stub — reserved for nearest-colour filter |

---

## Dependencies Installed

| Package | Version | Venv |
|---|---|---|
| insightface | 0.7.3 | pipeline-v2 |
| scikit-learn | (already present) | pipeline-v2 |
| opencv-python | 4.10.0 | pipeline-v2 |

GroundingDINO: not installed (weights/install handled externally per brief).
ByteTrack (`lap`): build failed — IoU fallback used instead.

---

## Known Limitations / Phase 3 Targets

1. **ByteTrack** — `lap` Cython build fails in this environment; greedy IoU tracker used as fallback.
   ReID across shots would require a proper multi-frame tracker.
2. **GroundingDINO weights absent** — Stage 6b skips entirely; `object_track` table will be empty
   until weights are placed at `/workspace/weights/groundingdino/`.
3. **`object_crop_qwen3vl` collection** — crop embeddings not yet wired (requires GroundingDINO running
   first, then a separate embed job).
4. **Visual cross-modal search** — still deferred (text query 4096d vs visual collection 2048d).
5. **Single test asset** — character clustering not validated end-to-end yet; requires re-ingest
   through the new enriching state.
