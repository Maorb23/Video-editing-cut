# CPU room echo cleaning

Selected before implementation: upstream DeepFilterNet v0.5.6, standard
DeepFilterNet3 ONNX archive (not the low-latency model), with the x86_64 Linux
musl `deep-filter` executable. The release files were fetched from upstream
over HTTPS and their SHA-256 values calculated locally on 2026-09-06.
Upstream does not publish independent SHA-256 digests for this release.

Model: https://raw.githubusercontent.com/Rikorose/DeepFilterNet/v0.5.6/models/DeepFilterNet3_onnx.tar.gz

SHA-256: `c94d91f70911001c946e0fabb4aa9adc37045f45a03b56008cb0c8244cb63616`

Executable: https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl

SHA-256: `70775e251eee44c0f2451a1e833326cf8bcbbe304d3e7cd12851e6fce72ef7da`

This is a speech enhancement model with reverb augmentation (`p_reverb=0.1`),
not a guarantee of complete echo removal for every room or music recording.
No EQ or alternative denoiser is substituted on failure. Real acoustic quality
should be evaluated on representative reverberant speech before production rollout.

The Rust executable uses the CPU Tract runtime. Its `-D` option removes 1,440
samples of latency at 48 kHz. The adapter pads the input before processing to
flush that latency and the last partial 480-sample hop, then restores the
original decoded audio sample count. Source video and rational project timing
remain independent of audio preprocessing.

Upstream source and model artifacts are distributed under the project's
MIT/Apache-2.0 license choice; the worker includes upstream MIT attribution.
See https://github.com/Rikorose/DeepFilterNet/tree/v0.5.6 .

The worker image bundles both artifacts at build time and validates their
hashes on startup and before each cleaning run. There is no runtime download.
The current image pin supports Linux x86_64. Local development can select paths
using `VIDEO_EDIT_DEREVERB_EXECUTABLE` and `VIDEO_EDIT_DEREVERB_MODEL`; these
overrides do not change the expected hashes. Installing the Python
`deepfilternet` extra alone does not enable this Rust CPU adapter.

The default processing deadline is 3,600 seconds for the entire extraction,
inference and normalization stage; diagnostic capture is limited to 65,536 bytes
per process. Cleaning writes `derived/dereverb/audio.wav` and `manifest.json`
as a single atomic directory publication inside the job. The manifest records
the source/output hashes, executable/model hashes and version, sample count,
sample rate, channels, delay compensation and processing time. Planning receives
only a capability summary, while the assembled plan and inspection report carry
the provenance. A revision with existing cleaning reruns preflight in its own job.

Run focused tests with `python -m unittest tests.test_dereverb` and the real
worker-toolchain fixture lane with
`xvfb-run -a python -m unittest tests.integration.test_dereverb_pipeline`.
