"""Pinned local speech assets; custom packs in data/voices are never downloaded or replaced."""
from __future__ import annotations

import hashlib
import logging
import threading

from ..vision.models import _fetch_to

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
ZERO_BASE = "https://huggingface.co/zeroweight-ai/ZeroTTS/resolve/18d6681518a4becaed3c125ceb3c7db59905fe86/"
MOSS_BASE = "https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX/resolve/ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae/"
# Published revisions verified against the installed model files.
FILES = {
    'config.json': (378, '3676da6a9f4dba7a8f2d106d5c1fb02c491391dc19c91dae893133a7e219d666'),
    'tokenizer.json': (191542, '4fd646e8a1fd6694cb9c876c914a516ef8dd84b4f84db2605be1a7388885ae91'),
    'null_voice_emb.npy': (30848, 'ec014c14f79e8fc16f4ca6ea557f33fa2cbd7d448b5de5f467aace9ca0321e9f'),
    'silence_frame.npy': (256, 'bb2628efa99ae02819b04ff7509ef43ddb3213a9fb0916e4e88b8d1a7c3a1810'),
    'voices/index.json': (3348, '13637f10cb361ee90d67ef0932126e8bf835b4b1ee6e038b9a769d62284456db'),
    'onnx/codec/codec_browser_onnx_meta.json': (14056, '32009d6ac1cd2663bbbf5c06d6835b3a862c02f7de121663071938f2edac8e92'),
    'onnx/codec/moss_audio_tokenizer_decode_full.onnx': (681902, '0fbbafe3fd4afa2a019af5c5ced204af6e2d1db044fa40f021525d2aee95b4ac'),
    'onnx/codec/moss_audio_tokenizer_decode_shared.data': (44198912, 'e69d52e0f4e84ca27850557ee54face46632d3a5a16c89bd246c7c408466dcad'),
    'onnx/codec/moss_audio_tokenizer_decode_step.onnx': (351400, '9527c86a29e1837edec1f74db57d5eeaadb3a715af3382703566460afed25855'),
    'onnx/codec/moss_audio_tokenizer_encode.data': (44507136, 'aa751265b2bab2887eac224484546b194875aa7494b607115439b3dc6b228a2c'),
    'onnx/codec/moss_audio_tokenizer_encode.onnx': (815775, 'eadea4a645abdcf98714c7aead122ee2ce7da6e080f9f80b977cd1ca8e19473a'),
    'onnx/local_frame_decode.onnx': (186743011, '3c4540ef4e69dcf604dc6f60f6f99f119189694ff5f31113def20406e274f025'),
    'onnx/prefix_step.onnx': (348303894, 'b7544c9fdd3535b21fe8dea855407180ad2084f9da298f8a153893522496565b'),
    'onnx/text_encoder.onnx': (323096725, 'd37557a4abe07953a02686a176664486e73838389a14ff4a3f36b47ac2bccd52'),
    'onnx/voice_encoder.onnx': (139853093, 'f6b84e65108d39bbff4f82927881c608778015fa36aba253c8336c08ab239f13'),
    'voices/baotrang/meta.json': (305, 'eb2f68dde7d10da196b23adf0e276f77f9b4304e07e6517fc42b7a52104133f1'),
    'voices/baotrang/voice.npz': (31262, 'abbc5807cc4767e7c62cc59f7b7cd04b4b7e0dba176086447a0c114578cf695a'),
    'voices/giahuy/meta.json': (284, '6cea4affa14ab3bcf1ceb6be3a5dab21271463db447f5486b8c5a94ec3e8714c'),
    'voices/giahuy/voice.npz': (31262, '3979a8369000eaaee1130801c1cacdadddc12e8d9a4a604be7430c4bcc6df32e'),
    'voices/hamy/meta.json': (270, '9b29222f8a8c4b73a6738fae2b62f322be3085f63888409fab93d3b6b1459b5d'),
    'voices/hamy/voice.npz': (31262, 'a864c127386f74976154c840875f6ad615d74c377768bc6edcb75e3278f228b7'),
    'voices/huuduc/meta.json': (301, '84869f386fe884418da285a22b95d6a79da5037d029425e44f81f5e9ce54ee19'),
    'voices/huuduc/voice.npz': (31262, 'd7a6370180263093eb9dd009661ba24b5309f0ba94ec18d8ff2323c80616a7eb'),
    'voices/kimoanh/meta.json': (303, '84f61033f18ca44dabdf6733c6e963678673b627eb9b3499ad91ea835d4f9e96'),
    'voices/kimoanh/voice.npz': (31262, '0aee413f25eae30123f930bd525d31a3b5a20c0803b4899305c2ff4a69466946'),
    'voices/maichi/meta.json': (260, '39cb20636913f2ad28294a4d2aaeb7fa811c0412a29713b8c0fec37bd2ae165c'),
    'voices/maichi/voice.npz': (31222, 'd49e0a406d3a005e95f0a19476426973946c4a97e0087eefd638de222913ae3c'),
    'voices/quangminh/meta.json': (282, 'eeb63a9835974d1122f9b6fb94bd5a8bfb3bcf7d718cf13ad75e5bc618a5d17f'),
    'voices/quangminh/voice.npz': (31262, '4d2acb18f831ade23ed2d02e7b749fa745c95e9ee305795c5ddeb75f93d35fdb'),
    'voices/tiendat/meta.json': (303, 'a62d71ef35fc9e6b01bae0ba3fdfbfeee660bb1052a52077dcbb09db7f2d0f21'),
    'voices/tiendat/voice.npz': (31262, 'a076fe7b938ad057652ffc4614678395edad90ab5cdc74546f3977636de15d0a'),
}


def ensure(cfg, *, creation=False, progress=None):
    """Fetch missing assets once, verify downloads, and publish each file atomically.

    Existing files with the expected size are kept for offline use. Interrupted downloads
    are retried on the next use; a failed download never replaces an installed file.
    """
    root = cfg.path("models/ZeroTTS")
    with _LOCK:
        for name, (size, digest) in FILES.items():
            encoder = name in ("onnx/voice_encoder.onnx", "onnx/codec/moss_audio_tokenizer_encode.onnx",
                               "onnx/codec/moss_audio_tokenizer_encode.data")
            if encoder and not creation:
                continue
            dest = root / name
            if dest.is_file() and dest.stat().st_size == size:
                continue
            url = (MOSS_BASE + dest.name if dest.name.startswith("moss_audio_tokenizer_encode.")
                   else ZERO_BASE + name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged = dest.with_name(dest.name + ".download")
            log.info("Downloading ZeroTTS asset %s", name)
            try:
                _fetch_to(url, staged, progress)
                with staged.open("rb") as stream:
                    hasher = hashlib.sha256()
                    while chunk := stream.read(1 << 20):
                        hasher.update(chunk)
                    actual = hasher.hexdigest()
                if staged.stat().st_size != size or actual != digest:
                    raise RuntimeError(f"ZeroTTS checksum mismatch: {name}; retry the download")
                staged.replace(dest)
            finally:
                staged.unlink(missing_ok=True)
                staged.with_name(staged.name + ".part").unlink(missing_ok=True)
