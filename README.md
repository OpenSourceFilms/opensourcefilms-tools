# Open Source Films: Filmmaking Tools

Tools built to support an independent feature film made without industry backing.
Its behind-the-scenes footage is part of the film's story, which means the production itself
generates a large, continuously growing body of raw media that needs to become searchable and
editable rather than sitting unused on disk.

This repo holds the media-evidence retrieval pipeline that grew out of that need. It turns raw
production footage into searchable, structured artifacts:

- ASR / transcription
- speaker- and entity-level enrichment (faces, objects, shot metadata, character clustering)
- vector embeddings + a search API over the resulting corpus
- a synthetic-query layer for natural-language lookups against the archive

**Historical, superseded project snapshot (October 2026).** This code is
retained as an engineering reference, not an active installation guide or
deployable release. The current public editorial-retrieval work is maintained
separately in [opensourcefilms-erf](https://github.com/OpenSourceFilms/opensourcefilms-erf).

**Do not deploy or run this repository's historical infrastructure/startup
scripts.** The original bootstrap contained shared default passwords,
unsafe authentication fallbacks and network-listener assumptions. We have
disabled the primary bootstrap/startup entrypoints in this public snapshot
so an accidental run fails before changing a system. They are preserved
solely to document earlier development. See [SECURITY.md](SECURITY.md).
If you reuse ideas or code, design a separate, authenticated deployment
and evaluate all current upstream licences and security requirements.

See `MASTER.md` for the canonical spec (stage responsibilities, invariants, execution rules) and
`PIPELINE_WRITE_AND_INTERACTION_MAP.md` / `METADATA_TAGS_AND_FLAGS_CONTRACT.md` for the artifact
contracts.

## GPU compute

Development and processing for this pipeline is done in collaboration with Runpod, who provide
GPU credits.

## Status

**Historical archive.** No production support, deployable service or current security assurance is provided. The private contemporary ERF service is separate.

## License

Code in this repository that is our own work is released under the [MIT License](LICENSE), free
for any use, including commercial, with attribution.

This pipeline depends on and integrates with third-party libraries, models, and tools, each under
their own license terms (some permissive, some more restrictive). Those third-party components are
**not** relicensed by this repository. See their own upstream projects for terms. A
third-party notice file will be added here as that audit completes.
