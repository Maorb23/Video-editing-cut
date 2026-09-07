"""Measurable pause evidence and conservative deterministic editing policy."""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from .errors import VideoEditingError
from .supervisor import ProcessSupervisor


@dataclass(frozen=True, init=False)
class SilenceSettings:
    window_seconds: float = .05
    quiet_percentile: float = .2
    calibration_margin_db: float = 8.
    threshold_min_db: float = -60.
    threshold_max_db: float = -30.
    fallback_threshold_db: float = -50.
    minimum_silence_seconds: float = .25
    speech_padding_seconds: float = .12

    def __init__(
        self,
        *,
        window_seconds: float = .05,
        quiet_percentile: float = .2,
        calibration_margin_db: float = 8.,
        threshold_min_db: float = -60.,
        threshold_max_db: float = -30.,
        fallback_threshold_db: float = -50.,
        minimum_silence_seconds: float = .25,
        speech_padding_seconds: float = .12,
        # Compatibility aliases for callers of the initial experimental API.
        margin_db: float | None = None,
        minimum_seconds: float | None = None,
        padding_seconds: float | None = None,
    ) -> None:
        values = {
            'window_seconds': window_seconds,
            'quiet_percentile': quiet_percentile,
            'calibration_margin_db': calibration_margin_db if margin_db is None else margin_db,
            'threshold_min_db': threshold_min_db,
            'threshold_max_db': threshold_max_db,
            'fallback_threshold_db': fallback_threshold_db,
            'minimum_silence_seconds': minimum_silence_seconds if minimum_seconds is None else minimum_seconds,
            'speech_padding_seconds': speech_padding_seconds if padding_seconds is None else padding_seconds,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        self.__post_init__()

    @property
    def margin_db(self) -> float:
        return self.calibration_margin_db

    @property
    def minimum_seconds(self) -> float:
        return self.minimum_silence_seconds

    @property
    def padding_seconds(self) -> float:
        return self.speech_padding_seconds

    def __post_init__(self):
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('silence settings must be finite numbers')
        if not (.01 <= self.window_seconds <= .2 and .05 <= self.quiet_percentile <= .4
                and 0 <= self.calibration_margin_db <= 20
                and -90 <= self.threshold_min_db <= self.fallback_threshold_db <= self.threshold_max_db <= -20
                and .05 <= self.minimum_silence_seconds <= 10 and 0 <= self.speech_padding_seconds <= 1):
            raise ValueError('silence settings exceed supported bounds')


def calibrate_noise(windows: list[float], settings: SilenceSettings) -> dict:
    values = sorted(max(-120., v) for v in windows if not math.isnan(v) and v <= 0)
    quiet = values[:max(1, math.ceil(len(values)*settings.quiet_percentile))]
    floor = statistics.median(quiet) if quiet else None
    spread = values[min(len(values)-1, int(len(values)*.8))] - floor if values else 0
    confidence = min(1., len(quiet)/10)*min(1., max(0., spread)/12)
    if floor is None or floor <= -100:
        confidence = 0.
    threshold = (max(settings.threshold_min_db, min(settings.threshold_max_db,
                                                     floor + settings.calibration_margin_db))
                 if confidence >= .6 else settings.fallback_threshold_db)
    return {'noise_floor_dbfs': floor, 'threshold_db': threshold, 'confidence': confidence,
            'fallback': confidence < .6, 'window_count': len(values), 'quiet_window_count': len(quiet),
            'method': 'rms-room-tone-lower-percentile/v1',
            'fallback_threshold_db': settings.fallback_threshold_db,
            'fallback_reason': 'insufficient room-tone separation' if confidence < .6 else None}


def analyze_audio(source, *, frame_rate, ffmpeg, threshold_db, settings, duration_seconds, supervisor,
                  source_fingerprint=None):
    from .silence import parse_silence_output
    supervisor = supervisor or ProcessSupervisor()
    if duration_seconds is None:
        from .probe import probe_one
        binary = Path(ffmpeg)
        ffprobe = str(binary.with_name('ffprobe.exe' if binary.suffix.lower() == '.exe' else 'ffprobe'))
        metadata = probe_one(source, ffprobe=ffprobe, supervisor=supervisor)
        duration_seconds = metadata.get('duration_seconds')
        if duration_seconds is None:
            raise VideoEditingError('silence analysis requires media duration', code='analysis_failed')
    windows, events = [], []
    def collect(line):
        match = re.search(r'lavfi.astats.Overall.RMS_level=(-?inf|-?\d+(?:\.\d+)?)', line)
        if match:
            if len(windows) >= 2_000_000:
                raise VideoEditingError('audio analysis window limit exceeded', code='resource_limit')
            windows.append(float(match[1]))
    measured = supervisor.run([ffmpeg, '-hide_banner', '-nostdin', '-i', str(source), '-map', '0:a:0',
        '-af', f'aresample=48000,asetnsamples=n={round(settings.window_seconds*48000)}:p=0,'
        'astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-',
        '-f', 'null', '-'], on_line=collect)
    if measured.returncode:
        raise VideoEditingError('audio calibration failed', code='analysis_failed')
    calibration = calibrate_noise(windows, settings)
    if threshold_db is not None:
        if type(threshold_db) not in (float, int) or not math.isfinite(threshold_db) or not -90 <= threshold_db <= -20:
            raise ValueError('threshold must be -90 through -20 dBFS')
        calibration.update(threshold_db=threshold_db, manual_override=True)
    selected = calibration['threshold_db']
    def collect_event(line):
        if 'silence_start:' in line or 'silence_end:' in line: events.append(line)
    detected = supervisor.run([ffmpeg, '-hide_banner', '-nostdin', '-i', str(source), '-map', '0:a:0',
        '-af', f'silencedetect=noise={selected:g}dB:d={settings.minimum_silence_seconds:g}',
        '-f', 'null', '-'], on_line=collect_event)
    if detected.returncode:
        raise VideoEditingError('silence detection failed', code='analysis_failed')
    evidence = parse_silence_output('\n'.join(events), source=source, frame_rate=frame_rate,
        threshold_db=selected, minimum_seconds=settings.minimum_silence_seconds,
        duration_seconds=duration_seconds)
    duration = Fraction(duration_seconds)
    evidence.update(
        version='1.0', status='complete', source_fingerprint=source_fingerprint,
        analyzed_duration_seconds=str(duration), analyzed_duration_frames=round(duration * frame_rate),
        calibration=calibration,
        settings={**asdict(settings), 'threshold_db': selected, 'selected_threshold_db': selected},
        detector={'name': 'ffmpeg.silencedetect', 'scope': 'full_source',
                  'threshold_db': selected, 'minimum_silence_seconds': settings.minimum_silence_seconds},
        evidence_id='analysis/silence.json',
    )
    for item in evidence['intervals']:
        item.update(candidate_id=item['id'], evidence_id=f"analysis/silence.json#{item['id']}",
                    threshold_db=selected, confidence=calibration['confidence'],
                    source_fingerprint=source_fingerprint,
                    contextual_evidence={'status': 'unavailable', 'confidence': 0., 'evidence_ids': []})
        item['suggestion'] = silence_policy(item, frame_rate, settings)
    return evidence


def silence_policy(candidate: dict, rate: Fraction, settings: SilenceSettings | None = None) -> dict:
    settings = settings or SilenceSettings()
    frames = candidate['end_frame']-candidate['start_frame']
    duration = Fraction(frames, 1)/rate
    context = candidate.get('contextual_evidence', candidate.get('context', {}))
    confidence = context.get('confidence', 0)
    reliable = type(confidence) in (int, float) and .8 <= confidence <= 1 and bool(context.get('evidence_ids'))
    retained = .24 if not reliable else .18
    reason = 'duration default; unavailable context, preserve speech padding'
    if reliable and context.get('emphasis_score') == 'high':
        retained, reason = .6, 'measured emphasis: preserve a longer pause'
    elif reliable and (context.get('same_speaker') is False or context.get('room_tone_difference') == 'high'):
        retained, reason = .4, 'speaker/room-tone boundary: preserve more pause'
    elif reliable and (context.get('speech_before') is False or context.get('speech_after') is False):
        retained, reason = .35, 'one-sided speech evidence: preserve an edge pause'
    elif reliable and context.get('sentence_boundary') is False:
        retained, reason = .32, 'within-sentence pause: preserve natural phrasing'
    padding = 2*round(Fraction(str(settings.speech_padding_seconds))*rate)
    target = min(frames, max(round(Fraction(str(retained))*rate), padding))
    action = 'keep' if duration < Fraction(35, 100) or target >= frames else 'shorten'
    if reliable and duration > Fraction(8, 10) and context.get('non_speech') is True and context.get('emphasis_score') == 'low':
        action, target, reason = 'remove', 0, 'long verified non-speech pause'
    transition = 'synchronized'
    if action != 'keep' and reliable and context.get('sentence_boundary') is not False and context.get('lips_visible_near_cut') is False and context.get('emphasis_score') == 'low' and context.get('visual_discontinuity') == 'low' and context.get('room_tone_difference') == 'low':
        if context.get('l_cut_safe') is True: transition = 'l_cut'
        elif context.get('j_cut_safe') is True: transition = 'j_cut'
    return {'candidate_id': candidate.get('id'), 'action': action, 'retained_frames': frames if action == 'keep' else target,
            'transition': transition, 'reason': reason, 'evidence_ids': [candidate.get('evidence_id', 'analysis/silence.json'), *context.get('evidence_ids', [])]}
