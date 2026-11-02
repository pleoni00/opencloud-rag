"""Fetch a pinned public ONNX encoder and verify every artifact before use."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
REPO = 'qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
COMMIT = 'faf4aa4225822f3bc6376869cb1164e8e3feedd0'
target = ROOT / '.private/model'
target.mkdir(parents=True, exist_ok=True)
manifest_path = ROOT / 'evidence/embedding-model.json'
if manifest_path.exists():
    previous = json.loads(manifest_path.read_text())
    if previous.get('repository') == REPO and previous.get('commit') == COMMIT and len(previous.get('artifacts', [])) == 5:
        valid = True
        for entry in previous['artifacts']:
            path = target / entry['name']
            if not path.is_file():
                valid = False
                break
            with path.open('rb') as stream:
                valid &= hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256']
        if valid:
            print('Pinned local model verified.', flush=True)
            raise SystemExit(0)
with urlopen(f'https://huggingface.co/api/models/{REPO}/revision/{COMMIT}?blobs=true', timeout=60) as response:
    metadata = json.load(response)
assert metadata['sha'] == COMMIT
artifacts = []
for artifact in metadata['siblings']:
    name = artifact['rfilename']
    if name not in ('model_optimized.onnx', 'tokenizer.json', 'config.json', 'special_tokens_map.json', 'tokenizer_config.json'):
        continue
    size = artifact['size']
    digest = artifact.get('lfs', {}).get('sha256')
    path = target / name
    if path.exists() and path.stat().st_size == size:
        with path.open('rb') as stream:
            valid = not digest or hashlib.file_digest(stream, 'sha256').hexdigest() == digest
        if valid:
            artifacts.append({'name': name, 'size': size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            continue
    partial = path.with_suffix(path.suffix + '.part')
    fd = os.open(partial, os.O_RDWR | os.O_CREAT, 0o600)
    os.ftruncate(fd, size)
    step = 4 * 1024 * 1024
    complete = set()
    for start in range(0, size, step):
        try:
            if os.lseek(fd, start, os.SEEK_DATA) <= start and os.lseek(fd, start, os.SEEK_HOLE) >= min(start + step, size):
                complete.add(start)
        except OSError:
            pass

    def segment(start):
        if start in complete:
            return
        end = min(start + step, size) - 1
        for attempt in range(6):
            try:
                req = Request(f'https://huggingface.co/{REPO}/resolve/{COMMIT}/{name}?download=true',
                              headers={'Range': f'bytes={start}-{end}'})
                with urlopen(req, timeout=90) as response:
                    if size > step and response.status != 206:
                        raise RuntimeError('Missing ranged response')
                    position = start
                    while position <= end:
                        data = response.read(min(1024 * 1024, end - position + 1))
                        if not data:
                            raise RuntimeError('Incomplete model artifact')
                        os.pwrite(fd, data, position)
                        position += len(data)
                return
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 20))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(segment, range(0, size, step)))
    finally:
        os.close(fd)
    with partial.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest:
        assert actual == digest, name
    else:
        raw = partial.read_bytes()
        assert hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest() == artifact['blobId'], name
    partial.rename(path)
    artifacts.append({'name': name, 'size': size, 'sha256': actual})
    print('Verified', name, flush=True)
manifest_path.write_text(json.dumps({'repository': REPO, 'commit': COMMIT, 'artifacts': artifacts}, indent=2))
