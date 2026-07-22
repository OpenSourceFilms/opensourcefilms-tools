# Open Source Films — Filmmaking Tools

Tools built to make **Life®**, an independent feature film made without industry backing.
Its behind-the-scenes footage is part of the film's story, which means the production itself
generates a large, continuously growing body of raw media that needs to become searchable and
editable rather than sitting unused on disk.

This repo holds the media-evidence retrieval pipeline that grew out of that need: it turns raw
production footage into searchable, structured artifacts —

- ASR / transcription
- speaker- and entity-level enrichment (faces, objects, shot metadata, character clustering)
- vector embeddings + a search API over the resulting corpus
- a synthetic-query layer for natural-language lookups against the archive

It's tooling built for a real production, published as it's built rather than as a finished
product — expect rough edges and active change.

See `MASTER.md` for the canonical spec (stage responsibilities, invariants, execution rules) and
`PIPELINE_WRITE_AND_INTERACTION_MAP.md` / `METADATA_TAGS_AND_FLAGS_CONTRACT.md` for the artifact
contracts.

## GPU compute

Development and processing for this pipeline is supported by GPU credits from
[Runpod](https://github.com/runpod).

## Status

Active development, in step with an ongoing film production. Interfaces and stage boundaries may
change without notice.
