# MLT mapping registry

Tested service baseline: MLT 7.28 as bundled with Shotcut 24.06. The compiler
pins its XML `version` and records catalog ID `mlt-7.28-shotcut-24.06`.

| Plan feature | MLT service/property |
| --- | --- |
| file producer | `avformat-novalidate` |
| still image | `qimage` |
| caption | transparent `color` producer + `dynamictext` |
| overlay/transform | `affine`, `transition.rect`, `transition.mix` |
| volume/fade | `volume`, `level` (keyframed for fade) |
| transition | tractor `luma` transition between clips on different tracks |
| track compositing | always-active `qtblend` transition |
| chroma key | `frei0r.bluescreen0r` |
| mask | `shape` |
| speed | reserved `timewarp`; structural duration must already be resolved |
| animated color | allow-listed `avfilter.colorbalance` properties and optional mask |
| parametric EQ | `avfilter.equalizer` with bounded typed bands |
| added reverb | `avfilter.aecho` with bounded room/mix parameters |
| dereverb | immutable derived audio producer; no MLT noise-reduction substitute |

Curated filters: `brightness`, `contrast`, `saturation`, `blur`, `sharpen`,
`grayscale`, `sepia`, and `white_balance`. The Python registry is authoritative
for allowed properties and exact services. Never pass through an arbitrary MLT
service or property from natural language.
