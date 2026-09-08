"""Resolve typed pause decisions into synchronized, frame-exact timeline edits."""
import json
import re
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

from .adaptive_silence import SilenceSettings, silence_policy
from .errors import VideoEditingError
from .operations import STRUCTURAL, resolve_timeline


PREFLIGHT_REQUIRED_MESSAGE = (
    'Preflight must be rerun with the requested settings before a pause-edit plan can be created.'
)


def requests_pause_edit(instruction: str, previous_plan: dict | None = None) -> bool:
    """Recognize pause-edit intent without asking the runtime model to infer preflight state."""
    requested = bool(re.search(
        r'\b(silence|silences|silent|pause|pauses|dead[ -]?air)\b', instruction, re.IGNORECASE,
    ))
    prior = (previous_plan or {}).get('analysis', {}).get('silence_decisions', [])
    return requested or bool(prior)


def require_silence_preflight(analysis, *, evidence_path: Path | None = None) -> dict:
    """Reject missing, stale, partial, or differently configured pause evidence."""
    def fail() -> None:
        raise VideoEditingError(PREFLIGHT_REQUIRED_MESSAGE, code='silence_preflight_required')

    data = getattr(analysis, 'data', analysis)
    if not isinstance(data, dict):
        fail()
    source = data.get('source', {})
    rate_data = data.get('timeline_policy', {}).get('frame_rate')
    configured = data.get('analysis_configuration', {}).get('silence', {})
    preflight = data.get('preflight', {}).get('silence', {})
    audio_evidence = data.get('audio_evidence', {})
    evidence = audio_evidence.get('silence') if isinstance(audio_evidence, dict) else None
    if not all(isinstance(value, dict) for value in (source, rate_data, configured, preflight, evidence)):
        fail()
    try:
        rate = Fraction(rate_data['numerator'], rate_data['denominator'])
        source_duration = Fraction(source['duration_seconds'])
        analyzed_duration = Fraction(evidence['analyzed_duration_seconds'])
        expected_frames = source['duration_frames']
        minimum = configured['minimum_silence_seconds']
        padding = configured['speech_padding_seconds']
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        fail()
    if type(expected_frames) is not int or expected_frames < 1:
        fail()
    if (preflight.get('status') != 'complete' or evidence.get('status') != 'complete'
            or evidence.get('source_fingerprint') != source.get('fingerprint')
            or evidence.get('frame_rate') != rate_data
            or analyzed_duration != source_duration
            or evidence.get('analyzed_duration_frames') != expected_frames
            or evidence.get('settings', {}).get('minimum_silence_seconds') != minimum
            or evidence.get('settings', {}).get('speech_padding_seconds') != padding
            or evidence.get('detector', {}).get('minimum_silence_seconds') != minimum
            or evidence.get('detector', {}).get('scope') != 'full_source'
            or preflight.get('minimum_silence_seconds') != minimum
            or preflight.get('speech_padding_seconds') != padding
            or preflight.get('analyzed_duration_seconds') != evidence.get('analyzed_duration_seconds')):
        fail()
    intervals = evidence.get('intervals')
    if not isinstance(intervals, list):
        fail()
    seen: set[str] = set()
    for candidate in intervals:
        if not isinstance(candidate, dict):
            fail()
        identifier = candidate.get('candidate_id')
        start, end = candidate.get('start_frame'), candidate.get('end_frame')
        if (not isinstance(identifier, str) or not identifier or identifier in seen
                or candidate.get('id') != identifier or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= expected_frames
                or candidate.get('duration_frames') != end - start
                or candidate.get('source_fingerprint') != source.get('fingerprint')
                or not isinstance(candidate.get('contextual_evidence'), dict)):
            fail()
        seen.add(identifier)
    relative = audio_evidence.get('evidence_id')
    if not isinstance(relative, str) or relative != preflight.get('evidence_id'):
        fail()
    if evidence_path is None and hasattr(analysis, 'path'):
        root = analysis.path.parent.parent if analysis.path.parent.name == 'analysis' else analysis.path.parent
        evidence_path = root / Path(relative)
    if evidence_path is not None:
        try:
            persisted = json.loads(evidence_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            fail()
        if persisted != evidence:
            fail()
    return evidence


def apply_silence_decisions(plan: dict, evidence: dict, decisions: list[dict]) -> dict:
    output = deepcopy(plan)
    rate = Fraction(**output['profile']['frame_rate'])
    if evidence.get('frame_rate') != output['profile']['frame_rate']:
        raise VideoEditingError('silence evidence uses a different frame rate', code='invalid_silence_evidence')
    asset = next((a for a in output['assets'] if a['id'] == evidence.get('asset_id','source')), None)
    if asset is None or (evidence.get('source_fingerprint') and evidence['source_fingerprint'] != asset['fingerprint']):
        raise VideoEditingError('silence evidence does not match source media', code='invalid_silence_evidence')
    for item in evidence.get('intervals', []):
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or type(item.get('start_frame')) is not int or type(item.get('end_frame')) is not int
                or not 0 <= item['start_frame'] < item['end_frame'] <= asset['duration_frames']):
            raise VideoEditingError('invalid candidate frame interval', code='invalid_silence_evidence')
    candidates = {item['id']: item for item in evidence.get('intervals', [])}
    if len(candidates) != len(evidence.get('intervals', [])):
        raise VideoEditingError('duplicate candidate evidence IDs', code='invalid_silence_evidence')
    settings = SilenceSettings(**{k:v for k,v in evidence.get('settings', {}).items() if k in SilenceSettings.__dataclass_fields__})
    original_tracks = resolve_timeline(output)
    originals = {c['id']: c for t in original_tracks for c in t['clips']}
    removals, resolved, seen = [], [], set()
    if not isinstance(decisions, list) or len(decisions) > 512:
        raise VideoEditingError('invalid silence decisions array', code='invalid_silence_decision')
    for decision in decisions:
        if (not isinstance(decision, dict) or set(decision) != {'candidate_id', 'asset_id', 'action'}
                or not isinstance(decision.get('candidate_id'), str) or decision['candidate_id'] not in candidates
                or decision.get('action') not in {'keep', 'shorten', 'remove'}
                or decision.get('asset_id') != evidence.get('asset_id', 'source')):
            raise VideoEditingError('invalid typed silence decision', code='invalid_silence_decision')
        key = decision['candidate_id']
        if key in seen:
            raise VideoEditingError('duplicate silence decision', code='invalid_silence_decision')
        seen.add(key)
        candidate = candidates[key]
        policy = silence_policy(candidate, rate, settings)
        if decision['action'] != 'keep' and decision['action'] != policy['action']:
            raise VideoEditingError('silence choice exceeds deterministic policy', code='unsafe_silence_decision')
        final = {**policy, **decision}
        if decision['action'] == 'keep':
            final.update(retained_frames=candidate['end_frame']-candidate['start_frame'], transition='synchronized', reason='explicitly preserve candidate pause')
        resolved.append(final)
        if decision['action'] == 'keep': continue
        retain = policy['retained_frames']
        begin, end = candidate['start_frame']+(retain+1)//2, candidate['end_frame']-retain//2
        matched = False
        for clip in originals.values():
            if clip['asset_id'] != decision['asset_id'] or not clip.get('enabled', True): continue
            a, b = max(begin, clip['source_in']), min(end, clip['source_in']+clip['duration'])
            if a < b:
                matched = True
                removals.append((clip['timeline_start']+a-clip['source_in'], clip['timeline_start']+b-clip['source_in']))
        if not matched:
            raise VideoEditingError('pause is outside active source ranges', code='invalid_silence_decision')
        final.update(source_remove_start=begin, source_remove_end=end)
    merged = []
    for a,b in sorted(removals):
        if merged and a <= merged[-1][1]: merged[-1] = (merged[-1][0], max(b,merged[-1][1]))
        else: merged.append((a,b))
    def ripple(frame): return frame-sum(max(0,min(frame,b)-a) for a,b in merged if a < frame)
    effects = [op for op in output.get('operations', []) if op['type'] not in STRUCTURAL]
    new_effects, mapping = [], {}
    tracks = deepcopy(original_tracks)
    for track in tracks:
        clips = []
        for clip in track['clips']:
            start, end = clip['timeline_start'], clip['timeline_start']+clip['duration']
            ranges, cursor = [], start
            for a,b in merged:
                if b <= cursor or a >= end: continue
                if a > cursor: ranges.append((cursor,a))
                cursor = min(end,max(cursor,b))
            if cursor < end: ranges.append((cursor,end))
            mapping[clip['id']] = []
            for part,(a,b) in enumerate(ranges):
                derived = dict(clip, id=clip['id'] if part == 0 else f"{clip['id']}__pause_{part:03d}",
                               timeline_start=ripple(a), source_in=clip['source_in']+a-start, duration=b-a)
                clips.append(derived)
                mapping[clip['id']].append((derived,a-start,b-start))
        track['clips'] = clips
    for op in effects:
        segments = mapping.get(op.get('target'))
        if segments is None:
            if merged and op.get('enabled', True):
                raise VideoEditingError('silence ripple conflicts with existing routing', code='unsupported_silence_combination')
            new_effects.append(op); continue
        original = originals[op['target']]
        changed = len(segments) != 1 or segments[0][1:] != (0,original['duration'])
        if changed and (op.get('keyframes') or op['type'] in {'speed','dereverb'}):
            raise VideoEditingError('pause intersects keyframes, speed, or cleaning; use a separate iteration', code='unsupported_silence_combination')
        for part,(clip,a,b) in enumerate(segments):
            begin = max(a,op.get('start',0))
            end = min(b,op.get('start',0)+op.get('duration',original['duration']-op.get('start',0)))
            if begin < end:
                new_effects.append(dict(op,id=op['id'] if part == 0 else f"{op['id']}__pause_{part:03d}",target=clip['id'],start=begin-a,duration=end-begin))
    output.update(tracks=tracks,operations=new_effects)
    output['analysis'] = {**output.get('analysis',{}), 'silence':deepcopy(evidence), 'silence_decisions':resolved,
                          'silence_removed_timeline_intervals':merged}
    # Only persisted reliable context can authorize an asymmetric join.
    from .audio_transitions import audio_routes
    for decision in resolved:
        if decision['action'] == 'keep' or decision['transition'] == 'synchronized': continue
        requested_transition = decision['transition']
        applied_transitions = []
        candidate = candidates[decision['candidate_id']]
        for track in tracks:
            if track['kind'] != 'video': continue
            for first,second in zip(track['clips'],track['clips'][1:]):
                if (first['asset_id'] != decision['asset_id'] or second['asset_id'] != decision['asset_id']
                        or first['source_in']+first['duration'] != decision['source_remove_start']
                        or second['source_in'] != decision['source_remove_end']): continue
                boundary = second['timeline_start']
                identifier = f"pause_join_{first['id']}_{second['id']}"
                context = candidate.get('contextual_evidence', candidate.get('context', {}))
                safety = {**context, 'id':identifier, 'from_clip_id':first['id'], 'to_clip_id':second['id'],
                          'picture_boundary_frame':boundary, 'source_evidence_ids':context['evidence_ids']}
                output['analysis'].setdefault('transition_safety',[]).append(safety)
                op = {'id':identifier,'type':'audio_transition','kind':requested_transition,
                      'from_clip_id':first['id'],'to_clip_id':second['id'],'picture_boundary_frame':boundary,
                      'av_offset_frames':4,'crossfade_frames':max(1,round(rate*Fraction(4,100))),'evidence_id':identifier}
                output['operations'].append(op)
                try: audio_routes(output,tracks)
                except VideoEditingError:
                    output['operations'].pop()
                    decision['reason'] += '; source/routing safety fallback'
                else:
                    applied_transitions.append(identifier)
        decision['transition_operation_ids'] = applied_transitions
        decision['transition'] = requested_transition if applied_transitions else 'synchronized'
    return output


def silence_review_markdown(evidence: dict, rate: Fraction) -> str:
    lines = ['## Detected silences','']
    def stamp(frame):
        seconds = float(Fraction(frame,1)/rate)
        return f'{int(seconds//60):02d}:{seconds%60:05.2f}'
    for item in evidence.get('intervals',[]):
        suggestion = item.get('suggestion') or silence_policy(item,rate)
        lines.extend([f"- {stamp(item['start_frame'])}–{stamp(item['end_frame'])} — {float(Fraction(item['end_frame']-item['start_frame'],1)/rate):.2f} s",
            f"  - threshold: {item.get('threshold_db',evidence.get('settings',{}).get('threshold_db'))} dBFS",
            f"  - calibration confidence: {item.get('confidence',0):.0%}",
            f"  - context: {item.get('contextual_evidence', item.get('context')) or 'unavailable'}", f"  - suggested action: {suggestion['action']}"])
    if not evidence.get('intervals'): lines.append('No candidate pauses met the detection criteria.')
    return '\n'.join(lines)+'\n'
