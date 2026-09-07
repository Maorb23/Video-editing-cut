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


_PADDING_VALUE = r'(?P<value>(?:\d+(?:\.\d+)?|\.\d+))\s*(?P<unit>milliseconds?|msecs?|ms|seconds?|secs?|s)'
_PADDING_PATTERNS = (
    re.compile(_PADDING_VALUE + r'\s+(?:of\s+)?(?:speech\s+)?padding\b', re.IGNORECASE),
    re.compile(r'\b(?:speech\s+)?padding\s*(?:of|to|=|:)?\s*' + _PADDING_VALUE, re.IGNORECASE),
)


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


def requested_speech_padding_seconds(instruction: str) -> float | None:
    """Extract a duration only when the instruction explicitly ties it to padding."""
    if not isinstance(instruction, str):
        return None
    values: set[Fraction] = set()
    for pattern in _PADDING_PATTERNS:
        for match in pattern.finditer(instruction):
            value = Fraction(match.group('value'))
            if match.group('unit').lower() in {'millisecond', 'milliseconds', 'msec', 'msecs', 'ms'}:
                value /= 1000
            values.add(value)
    if not values:
        return None
    if len(values) != 1:
        raise VideoEditingError('instruction contains conflicting speech padding values',
                                code='invalid_silence_settings')
    return float(values.pop())


def silence_settings_for_instruction(
    base: SilenceSettings,
    instruction: str,
    *,
    inherited_padding_seconds: float | None = None,
) -> SilenceSettings:
    """Resolve instruction padding over inherited and deployment defaults."""
    requested = requested_speech_padding_seconds(instruction)
    padding = requested if requested is not None else (
        inherited_padding_seconds if inherited_padding_seconds is not None else base.speech_padding_seconds
    )
    try:
        return SilenceSettings(**{**asdict(base), 'speech_padding_seconds': padding})
    except ValueError as exc:
        raise VideoEditingError(
            'speech padding must be between 0 and 1 second per side',
            code='invalid_silence_settings',
        ) from exc


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


def detect_quiet_intervals(windows: list[tuple[Fraction, float]], *, threshold_db: float,
                           window_seconds: float, minimum_seconds: float,
                           duration_seconds: Fraction) -> list[tuple[Fraction, Fraction]]:
    """Group calibrated quiet RMS windows into deterministic half-open intervals."""
    window_duration = Fraction(str(window_seconds))
    minimum_duration = Fraction(str(minimum_seconds))
    intervals: list[tuple[Fraction, Fraction]] = []
    start: Fraction | None = None
    end: Fraction | None = None
    for timestamp, level in windows:
        quiet = level <= threshold_db
        window_end = min(duration_seconds, timestamp + window_duration)
        if quiet:
            if start is None:
                start = timestamp
            end = window_end
        elif start is not None and end is not None:
            if end - start >= minimum_duration:
                intervals.append((start, end))
            start = end = None
    if start is not None and end is not None and end - start >= minimum_duration:
        intervals.append((start, end))
    return intervals


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
    windows: list[tuple[Fraction, float]] = []
    pending_timestamp: Fraction | None = None
    def collect(line):
        nonlocal pending_timestamp
        timestamp = re.search(r'pts_time:(-?\d+(?:\.\d+)?)', line)
        if timestamp:
            pending_timestamp = Fraction(timestamp.group(1))
        match = re.search(r'lavfi.astats.Overall.RMS_level=(-?inf|-?\d+(?:\.\d+)?)', line)
        if match and pending_timestamp is not None:
            if len(windows) >= 2_000_000:
                raise VideoEditingError('audio analysis window limit exceeded', code='resource_limit')
            windows.append((pending_timestamp, float(match[1])))
            pending_timestamp = None
    measured = supervisor.run([ffmpeg, '-hide_banner', '-nostdin', '-i', str(source), '-map', '0:a:0',
        '-af', f'aresample=48000,asetnsamples=n={round(settings.window_seconds*48000)}:p=0,'
        'astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-',
        '-f', 'null', '-'], on_line=collect)
    if measured.returncode:
        raise VideoEditingError('audio calibration failed', code='analysis_failed')
    calibration = calibrate_noise([level for _, level in windows], settings)
    if threshold_db is not None:
        if type(threshold_db) not in (float, int) or not math.isfinite(threshold_db) or not -90 <= threshold_db <= -20:
            raise ValueError('threshold must be -90 through -20 dBFS')
        calibration.update(threshold_db=threshold_db, manual_override=True)
    selected = calibration['threshold_db']
    duration = Fraction(duration_seconds)
    intervals = detect_quiet_intervals(
        windows, threshold_db=selected, window_seconds=settings.window_seconds,
        minimum_seconds=settings.minimum_silence_seconds, duration_seconds=duration,
    )
    events = '\n'.join(
        f'silence_start: {float(start):.12f}\nsilence_end: {float(end):.12f}'
        for start, end in intervals
    )
    evidence = parse_silence_output(events, source=source, frame_rate=frame_rate,
        threshold_db=selected, minimum_seconds=settings.minimum_silence_seconds,
        duration_seconds=duration_seconds)
    evidence.update(
        version='1.0', kind='rms_window_silence', status='complete', source_fingerprint=source_fingerprint,
        analyzed_duration_seconds=str(duration), analyzed_duration_frames=round(duration * frame_rate),
        calibration=calibration,
        settings={**asdict(settings), 'threshold_db': selected, 'selected_threshold_db': selected},
        detector={'name': 'rms_window_threshold/v1', 'scope': 'full_source',
                  'threshold_db': selected, 'window_seconds': settings.window_seconds,
                  'window_count': len(windows),
                  'minimum_silence_seconds': settings.minimum_silence_seconds},
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
