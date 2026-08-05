# Open Source Films: Filmmaking Tools

> **Note:** this repository holds an earlier generation of Open Source Films' media-retrieval
> pipeline. Current development and the more complete public architecture live in
> [opensourcefilms-erf](https://github.com/OpenSourceFilms/opensourcefilms-erf). This repo is kept
> public for its development lineage and for code not yet carried over, not as the canonical entry
> point.

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

It's tooling built for a real production, published as it's built rather than as a finished
product. Expect rough edges and active change.

See `MASTER.md` for the canonical spec (stage responsibilities, invariants, execution rules) and
`PIPELINE_WRITE_AND_INTERACTION_MAP.md` / `METADATA_TAGS_AND_FLAGS_CONTRACT.md` for the artifact
contracts.

## GPU compute

Development and processing for this pipeline is done in collaboration with Runpod, who provide
GPU credits.

## Status

Active development, in step with an ongoing film production. Interfaces and stage boundaries may
change without notice.

## License

Code in this repository that is our own work is released under the [MIT License](LICENSE), free
for any use, including commercial, with attribution.

This pipeline depends on and integrates with third-party libraries, models, and tools, each under
their own license terms (some permissive, some more restrictive). Those third-party components are
**not** relicensed by this repository. See their own upstream projects for terms. A
third-party notice file will be added here as that audit completes.
