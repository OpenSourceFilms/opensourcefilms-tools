from pathlib import Path
import yaml

cfg_path = Path("/workspace/pipeline/conf/pipeline.yaml")
cfg = yaml.safe_load(cfg_path.read_text())

cpu = cfg.setdefault("cpu", {})

# Preserve existing cpu keys; only replace cpu.asr
cpu["asr"] = {
    "engine": "whispercpp",
    "whispercpp": {
        "bin": "/workspace/pipeline/bin/asr_whispercpp.sh",
        "model": "/workspace/pipeline/models/whispercpp/ggml-small.en.bin",
        "threads": 4,
        "output_formats": ["srt", "vtt"],
        "time_budget_seconds": 900,
    }
}

cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
print("OK: patched cpu.asr to whispercpp in /workspace/pipeline/conf/pipeline.yaml")
