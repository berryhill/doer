#!/usr/bin/env python3
"""Explicit, offline-build-independent staging of the pinned English Laya checkpoint."""
import argparse
import json
from pathlib import Path

REVISION = '55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851'
REPOSITORY = 'convaiinnovations/laya'
PATTERNS = ['rl_agent_config.json', 'model.safetensors', 'tokenizer/*', 'encoder/*']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    path = args.destination.resolve()
    if path.exists() and any(path.iterdir()):
        parser.error('destination must be empty; never overwrite existing checkpoints')
    from huggingface_hub import snapshot_download
    path.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=REPOSITORY, revision=REVISION, local_dir=str(path),
                      allow_patterns=PATTERNS, token=False)
    for required in ('rl_agent_config.json', 'model.safetensors', 'tokenizer', 'encoder'):
        if not (path / required).exists():
            raise RuntimeError('incomplete checkpoint: ' + required)
    (path / 'doer-provenance.json').write_text(json.dumps({'repository': REPOSITORY, 'revision': REVISION}) + '\n')
    print('Staged pinned English checkpoint at', path)


if __name__ == '__main__':
    main()
