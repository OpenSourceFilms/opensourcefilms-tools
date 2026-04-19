"""
Stage 3b: run_asr
Detects language, routes to faster-whisper large-v3 (GPU).
Diarizes if configured. Writes TranscriptTurn and Sentence rows.
On completion, triggers embed if OCR is also done (atomic CAS on state).
"""
import logging
import os
import re
import subprocess
import tempfile

from sqlalchemy import text

from config.settings import settings
from db.sync_session import get_sync_db
from db.models import Asset, Shot, TranscriptTurn, Sentence
from ingest.gpu_pod_manager import gpu_available

logger = logging.getLogger(__name__)
WHISPER_VERSION = "large-v3"


def _to_wav(video_path: str, out_path: str, duration_s: int = 0):
    cmd = ["ffmpeg", "-y", "-i", video_path, "-ar", "16000", "-ac", "1"]
    if duration_s:
        cmd += ["-t", str(duration_s)]
    cmd.append(out_path)
    subprocess.run(cmd, capture_output=True, check=True, timeout=600)


def _detect_language(wav_path: str) -> str:
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        _, info = model.transcribe(wav_path, language=None, beam_size=1, best_of=1)
        return info.language or "en"
    except Exception as exc:
        logger.warning("[asr] lang detect failed (%s), defaulting to en", exc)
        return "en"


def _transcribe_whisper(wav_path: str) -> list[dict]:
    from faster_whisper import WhisperModel
    device = "cuda" if gpu_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    model_name = settings.whisper_fallback_model  # large-v3 on GPU
    model = WhisperModel(
        model_name, device=device, compute_type=compute,
        download_root=settings.whisper_fallback_dir,
    )
    segments, _ = model.transcribe(wav_path, word_timestamps=True, beam_size=5)
    return [
        {"text": w.word.strip(), "start": float(w.start), "end": float(w.end)}
        for seg in segments for w in (seg.words or [])
    ]


def _diarize(wav_path: str) -> list[dict]:
    try:
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=settings.hf_token or None,
        )
        if gpu_available():
            import torch
            pipeline = pipeline.to(torch.device("cuda"))
        diarization = pipeline(wav_path)
        return [
            {"start": t.start, "end": t.end, "speaker": spk}
            for t, _, spk in diarization.itertracks(yield_label=True)
        ]
    except Exception as exc:
        logger.warning("[asr] diarization failed (%s), single speaker", exc)
        return []


def _speaker_at(t: float, diarization: list[dict]) -> str | None:
    for seg in diarization:
        if seg["start"] <= t <= seg["end"]:
            return seg["speaker"]
    return None


def _words_to_turns(words: list[dict], diarization: list[dict], pause: float) -> list[dict]:
    if not words:
        return []
    turns, cur = [], [words[0]]
    cur_spk = _speaker_at(words[0]["start"], diarization)
    for w in words[1:]:
        spk = _speaker_at(w["start"], diarization)
        if spk != cur_spk or (w["start"] - cur[-1]["end"]) > pause:
            turns.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                          "speaker": cur_spk, "words": cur})
            cur, cur_spk = [w], spk
        else:
            cur.append(w)
    turns.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                  "speaker": cur_spk, "words": cur})
    return turns


def _turn_to_sentences(turn: dict) -> list[dict]:
    text_str = " ".join(w["text"] for w in turn["words"])
    parts = re.split(r"(?<=[.!?])\s+", text_str.strip())
    sents, wi = [], 0
    ws = turn["words"]
    for part in parts:
        n = len(part.split())
        if n == 0:
            continue
        s_idx = min(wi, len(ws) - 1)
        e_idx = min(wi + n - 1, len(ws) - 1)
        sents.append({"text": part, "start": ws[s_idx]["start"], "end": ws[e_idx]["end"]})
        wi += n
    return sents or [{"text": text_str, "start": turn["start"], "end": turn["end"]}]


def run_asr(asset_id: str):
    wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_tmp.close()

    with get_sync_db() as db:
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            logger.error("[asr] asset not found: %s", asset_id)
            return

        video_path = asset.proxy_path or asset.original_path
        logger.info("[asr] start asset_id=%s", asset_id)

        try:
            _to_wav(video_path, wav_tmp.name)

            sample_wav = wav_tmp.name + ".sample.wav"
            _to_wav(video_path, sample_wav, settings.asr_language_sample_s)
            language = _detect_language(sample_wav)
            try:
                os.unlink(sample_wav)
            except OSError:
                pass
            logger.info("[asr] language=%s asset_id=%s", language, asset_id)

            words = _transcribe_whisper(wav_tmp.name)
            producer_model = "faster-whisper"
            producer_version = settings.whisper_fallback_model

            diarization = _diarize(wav_tmp.name) if settings.diarize else []
            turns = _words_to_turns(words, diarization, settings.min_speaker_pause_s)

            shots = (
                db.query(Shot)
                .filter(Shot.asset_id == asset_id, Shot.deleted_at.is_(None))
                .order_by(Shot.shot_index)
                .all()
            )

            def _shot_for(pts: float) -> str | None:
                for s in shots:
                    if s.start_pts <= pts < s.end_pts:
                        return s.id
                return None

            for i, turn in enumerate(turns):
                tt = TranscriptTurn(
                    asset_id=asset_id,
                    shot_id=_shot_for(turn["start"]),
                    turn_index=i,
                    start_pts=round(turn["start"], 4),
                    end_pts=round(turn["end"], 4),
                    speaker_label=turn["speaker"],
                    text=" ".join(w["text"] for w in turn["words"]).strip(),
                    language=language,
                    confidence=0.92,
                    producer_model=producer_model,
                    producer_version=producer_version,
                )
                db.add(tt)
                db.flush()

                for j, sent in enumerate(_turn_to_sentences(turn)):
                    db.add(Sentence(
                        turn_id=tt.id,
                        asset_id=asset_id,
                        sentence_index=j,
                        start_pts=round(sent["start"], 4),
                        end_pts=round(sent["end"], 4),
                        text=sent["text"].strip(),
                        producer_model=producer_model,
                        producer_version=producer_version,
                    ))

            db.commit()
            logger.info("[asr] done asset_id=%s turns=%d model=%s", asset_id, len(turns), producer_model)

            # Atomic: mark asr_done=true, transition to embedding if ocr_done too
            row = db.execute(
                text("""
                    UPDATE asset
                    SET asr_done = true,
                        ingest_state = CASE WHEN ocr_done THEN 'embedding' ELSE ingest_state END
                    WHERE id = :id
                    RETURNING ingest_state
                """),
                {"id": asset_id},
            ).fetchone()
            db.commit()

            if row and row[0] == "embedding":
                logger.info("[asr] both asr+ocr done → queuing embed asset_id=%s", asset_id)
                _enqueue_embed(asset_id)

        except Exception as exc:
            logger.exception("[asr] failed asset_id=%s", asset_id)
            asset = db.query(Asset).filter(Asset.id == asset_id).first()
            if asset:
                asset.ingest_state = "failed"
                asset.ingest_error = f"asr: {exc}"
                db.commit()
            raise
        finally:
            try:
                os.unlink(wav_tmp.name)
            except OSError:
                pass


def _enqueue_embed(asset_id: str):
    from workers.rq_config import embed_queue
    from ingest.embed import embed_asset
    embed_queue.enqueue(embed_asset, asset_id, job_timeout=7200)
