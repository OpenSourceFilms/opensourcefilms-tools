# Third-party dependencies and licensing boundaries

**Status:** Historical engineering archive, reviewed 26 October 2026.
This document identifies software used or referenced in the former pipeline.
It is **not** a complete software bill of materials for the original
deployment, a fresh-runtime licence audit, or permission to reuse unrelated
model weights, data or binaries. See [SECURITY.md](SECURITY.md) before using
any historical scripts.

The repository's [MIT licence](LICENSE) covers material OSF has authority
to license, not independently owned upstream code, models or assets.

## Source licences checked against official upstream files

| Referenced component | Published upstream source licence | What is and is not included here |
|---|---|---|
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE) | MIT, the ggml authors | `bin/whispercpp` is a **681-byte OSF Bash launcher**, not a vendored whisper.cpp executable. The actual `whisper-cli` binary, its GGML model, and optional Silero VAD weights are looked up at external historical `/workspace` paths. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE) | MIT, SYSTRAN | Referenced in `ingest/asr.py` and requirements; Whisper model weights and any Hugging Face assets must be licensed separately. |
| [Silero VAD](https://github.com/snakers4/silero-vad/blob/master/LICENSE) | MIT, Silero Team | VAD model is an **external file** referenced by `bin/asr_whispercpp.sh`, not included in this repository. Check the exact model and packaging before redistributing. |
| [GroundingDINO](https://github.com/IDEA-Research/GroundingDINO/blob/main/LICENSE) | Apache-2.0 | `ingest/objects.py` requires an external `swint_ogc` checkpoint; the published repo contains no checkpoint or upstream model implementation. |
| [pyannote-audio](https://github.com/pyannote/pyannote-audio/blob/develop/LICENSE) | MIT | `ingest/asr.py` optionally loads a separate pretrained diarization pipeline. Gated model access, model terms and speaker-data rights are **not** covered by this source-code licence. |
| [Qdrant](https://github.com/qdrant/qdrant/blob/master/LICENSE) | Apache-2.0 | Referenced through client libraries and historical setup; server binaries are not bundled here. |
| [TransNetV2](https://github.com/soCzech/TransNetV2/blob/master/LICENSE) | MIT | Historical scene-detection script loads `transnetv2_pytorch` and **package-bundled pretrained weights**; the third-party wheel and its embedded checkpoint were not independently audited. The upstream repository licence is not sufficient evidence about that wheel. |

These statements describe **upstream source licences**, not necessarily
every installed wheel, compiled FFmpeg build, checkpoint, model derivative,
dataset or other component. Some models used elsewhere in the pipeline
may have non-commercial restrictions despite permissive source libraries.

## External dependencies needing their own checks

- **InsightFace / buffalo_l:** `ingest/faces.py` references the
  `insightface/buffalo_l` model pack. Common pretrained face-model packs
  carry **research/non-commercial restrictions** independently of the
  InsightFace Python library. Do not regard them as cleared for commercial
  use without verifying the exact pack and obtaining permission if needed.
- **E5 embeddings:** `bin/stageB1_embed_e5.py` defaults to
  `intfloat/e5-small-v2`; an ONNX variant references separately stored
  `model.onnx` and `tokenizer.json`. The exact checkpoint, converted
  ONNX weights, tokenizer and their licence/revision are not included or
  independently verified by this release.
- **Whisper model packs:** `large-v3`, `tiny`, and the
  `ggml-small.en.bin` model referred to in the scripts must be obtained
  separately. Do not infer their licence from the associated open-source
  inference wrappers.
- **FFmpeg, OpenCV wheels, model frameworks and services:** licence
  obligations may vary with linked dependencies, binary distributions,
  compile flags, server editions and installed versions. Historical
  requirements and environment locks are **not** a certification of the
  particular binaries used by another operator.
- **Other dependencies:** the historic scripts integrate PostgreSQL,
  Redis/RQ, object storage, Neo4j/Milvus, Hugging Face packages and other
  components. This list is not exhaustive; check their actual releases
  before building or distributing a combined system.

## One bundled media fixture awaiting provenance

The tree includes `bin/silence.wav` (32,044 bytes). Its original
generation method, embedded metadata and chain of title have **not**
been independently established by this review. It is a small historical
audio fixture, **not** a pretrained model or an assertion of third-party
licensing permission. Anyone redistributing the complete archive should
confirm its provenance or replace it with a freshly generated, documented
synthetic silence fixture. The file has been left untouched here.

## Redistribution and security boundaries

- No upstream `whisper-cli` binary, model weights or other named third-party
  executable is distributed by the checked current tree. The local wrapper
  merely references external paths; this does **not** clear historical
  commits, external storage or unidentified embedded content.
- OSF does not claim authorship of the upstream models, libraries, standards,
  or techniques used in the historical pipeline.
- The historical bootstrap/startup scripts are deliberately disabled to
  prevent unsafe deployment. **Do not treat an upstream MIT/Apache grant as
  evidence this old service configuration is safe to run.**
- When reusing a component, identify the precise version, preserve its
  original copyright and required notices, verify model-weight and
  media-data rights, and comply with any distribution obligations.
- This register cannot establish the ownership of every line in the
  archived repository. Contributions or imported material require their
  own chain-of-title check where relevant.

This document is a bounded technical provenance record, not legal advice.
