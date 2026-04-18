#!/usr/bin/env python3
"""
Direct (non-queued) ingest runner for testing/debugging.
Runs all pipeline stages synchronously for one asset.
Usage: python run_ingest_direct.py <asset_id>
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("direct_ingest")

from db.sync_session import get_sync_db
from db.models import Asset

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <asset_id>")
        sys.exit(1)

    asset_id = sys.argv[1]

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("Asset not found: %s", asset_id)
            sys.exit(1)
        # Reset to pending
        asset.ingest_state = "pending"
        asset.ingest_error = None
        asset.shot_count = None
        db.commit()

    logger.info("=== Stage 1: probe_asset ===")
    from ingest.probe import _ffprobe, _extract_meta, _sha256
    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        asset.ingest_state = "probing"
        db.commit()
        probe_data = _ffprobe(asset.original_path)
        meta = _extract_meta(probe_data)
        sha = _sha256(asset.original_path)
        asset.duration_s = meta["duration_s"]
        asset.fps = meta["fps"]
        asset.width = meta["width"]
        asset.height = meta["height"]
        asset.codec = meta["codec"]
        asset.container = meta["container"]
        asset.sha256 = sha
        asset.ingest_state = "segmenting"
        db.commit()
        logger.info("probe done: %s, %ss, %sfps, %sx%s", meta["codec"],
                    meta["duration_s"], meta["fps"], meta["width"], meta["height"])

    logger.info("=== Stage 2: segment_shots ===")
    from ingest.segment import _run_transnetv2
    with get_sync_db() as db:
        from db.models import Shot
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        video_path = asset.proxy_path or asset.original_path
        boundaries = _run_transnetv2(video_path, asset.duration_s or 0)
        for idx, (start, end) in enumerate(boundaries):
            db.add(Shot(
                asset_id=asset_id, shot_index=idx,
                start_pts=start, end_pts=end,
                producer_model="transnetv2", producer_version="2.0.0",
            ))
        asset.shot_count = len(boundaries)
        asset.ingest_state = "extracting"
        db.commit()
        logger.info("segment done: %d shots", len(boundaries))

    logger.info("=== Stage 3a: extract_keyframes ===")
    from ingest.keyframes import _extract_frames, _compute_phash
    import store.client as store
    import os, tempfile
    from db.models import Keyframe, Shot as ShotModel

    with get_sync_db() as db:
        from config.settings import settings
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        shots = db.query(ShotModel).filter(ShotModel.asset_id == asset_id).order_by(ShotModel.shot_index).all()
        video_path = asset.proxy_path or asset.original_path
        seen = set()
        total = 0
        for shot in shots:
            frames = _extract_frames(video_path, shot.start_pts, shot.end_pts, settings.keyframes_per_shot)
            for pts, tmp_path in frames:
                try:
                    phash = _compute_phash(tmp_path)
                    if phash in seen:
                        continue
                    seen.add(phash)
                    key = f"{asset_id}/{shot.id}/{pts:.3f}.jpg"
                    store.upload_file(tmp_path, "media-keyframes", key)
                    db.add(Keyframe(
                        shot_id=shot.id, asset_id=asset_id,
                        pts=pts, path=f"media-keyframes/{key}", phash=phash,
                    ))
                    total += 1
                finally:
                    try: os.unlink(tmp_path)
                    except: pass
        db.commit()
        logger.info("keyframes done: %d written", total)

    logger.info("=== Stage 3b: run_asr (faster-whisper fallback, CPU) ===")
    from ingest.asr import _to_wav, _detect_language, _transcribe_whisper, _diarize, _words_to_turns, _turn_to_sentences
    from db.models import TranscriptTurn, Sentence, Shot as ShotModel2
    from config.settings import settings as cfg

    wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_tmp.close()
    try:
        with get_sync_db() as db:
            asset = db.query(Asset).filter(Asset.id == asset_id).first()
            video_path = asset.proxy_path or asset.original_path
            _to_wav(video_path, wav_tmp.name)
            language = _detect_language(wav_tmp.name)
            logger.info("language detected: %s", language)

            words = _transcribe_whisper(wav_tmp.name)
            logger.info("transcription done: %d words", len(words))

            diarization = []  # skip diarization for speed in direct test
            turns = _words_to_turns(words, diarization, cfg.min_speaker_pause_s)
            shots = db.query(ShotModel2).filter(ShotModel2.asset_id == asset_id).order_by(ShotModel2.shot_index).all()

            def _shot_for(pts):
                for s in shots:
                    if s.start_pts <= pts < s.end_pts: return s.id
                return None

            for i, turn in enumerate(turns):
                tt = TranscriptTurn(
                    asset_id=asset_id, shot_id=_shot_for(turn["start"]),
                    turn_index=i, start_pts=round(turn["start"],4),
                    end_pts=round(turn["end"],4), speaker_label=turn["speaker"],
                    text=" ".join(w["text"] for w in turn["words"]).strip(),
                    language=language, confidence=0.85,
                    producer_model="faster-whisper", producer_version="small.en",
                )
                db.add(tt); db.flush()
                for j, sent in enumerate(_turn_to_sentences(turn)):
                    db.add(Sentence(
                        turn_id=tt.id, asset_id=asset_id, sentence_index=j,
                        start_pts=round(sent["start"],4), end_pts=round(sent["end"],4),
                        text=sent["text"].strip(),
                        producer_model="faster-whisper", producer_version="small.en",
                    ))
            db.commit()
            logger.info("ASR done: %d turns", len(turns))
    finally:
        try: os.unlink(wav_tmp.name)
        except: pass

    logger.info("=== Stage 4: run_ocr (skipping — PaddleOCR not installed) ===")
    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        asset.ingest_state = "complete"
        db.commit()
        logger.info("Asset state → complete")

    # Final summary
    from db.models import TranscriptTurn as TT, Sentence as SS, Keyframe as KF, Shot as SH
    with get_sync_db() as db:
        shots_n = db.query(SH).filter(SH.asset_id == asset_id).count()
        kf_n = db.query(KF).filter(KF.asset_id == asset_id).count()
        turns_n = db.query(TT).filter(TT.asset_id == asset_id).count()
        sents_n = db.query(SS).filter(SS.asset_id == asset_id).count()

    print("\n" + "="*60)
    print(f"INGEST COMPLETE — asset_id={asset_id}")
    print(f"  shots:           {shots_n}")
    print(f"  keyframes:       {kf_n}")
    print(f"  transcript_turns:{turns_n}")
    print(f"  sentences:       {sents_n}")
    print("="*60)


if __name__ == "__main__":
    main()
