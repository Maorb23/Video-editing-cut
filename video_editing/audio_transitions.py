"""Constrained split edits; all source mapping and routing belongs to Python."""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction

from .errors import VideoEditingError


def audio_routes(plan: dict, tracks: list[dict]) -> dict[str, dict]:
    """Validate joins and return independently timed audio clips (half-open frames)."""
    operations = [op for op in plan.get('operations', []) if op['type'] == 'audio_transition' and op.get('enabled', True)]
    if not operations:
        return {}
    clips = {c['id']: c for t in tracks for c in t['clips'] if c.get('enabled', True)}
    owners = {c['id']: t for t in tracks for c in t['clips']}
    assets = {a['id']: a for a in plan['assets']}
    rate = Fraction(**{ 'numerator': plan['profile']['frame_rate']['numerator'],
                        'denominator': plan['profile']['frame_rate']['denominator']})
    routes, boundaries = {}, set()
    analysis = plan.get('analysis', {})
    safety = analysis.get('transition_safety', []) if isinstance(analysis, dict) else []
    if not isinstance(safety, list): safety = []
    def fail(message, code='unsafe_audio_transition'):
        raise VideoEditingError(message, code=code)
    for op in sorted(operations, key=lambda value: value['picture_boundary_frame']):
        first, second = clips.get(op['from_clip_id']), clips.get(op['to_clip_id'])
        if first is None or second is None:
            fail('audio transition references a missing or disabled clip')
        owner = owners[first['id']]
        boundary = op['picture_boundary_frame']
        if (owner is not owners[second['id']] or owner['kind'] != 'video' or owner.get('muted')
                or first['timeline_start'] + first['duration'] != boundary or second['timeline_start'] != boundary):
            fail('audio transitions require adjacent audible video clips at the declared boundary')
        signature = (owner['id'], boundary)
        if signature in boundaries:
            fail('duplicate audio transition boundary', 'overlapping_audio_routing')
        boundaries.add(signature)
        for clip in (first, second):
            asset = assets[clip['asset_id']]
            if asset.get('probe', {}).get('audio') is None:
                fail('audio transition requires probed audio streams')
            if any(effect.get('enabled', True) and effect.get('target') == clip['id'] and effect['type'] in
                   {'speed', 'dereverb', 'fade_audio', 'parametric_eq', 'reverb'} for effect in plan.get('operations', [])):
                fail('split edits cannot combine with speed, cleaning, or timed audio effects')
            if any(effect.get('enabled',True) and effect.get('target') == clip['id'] and effect['type'] == 'volume'
                   and (effect.get('start',0) or 'duration' in effect) for effect in plan.get('operations',[])):
                fail('split edits require constant full-clip volume; timed volume conflicts with handle routing')
            for track in tracks:
                if track is owner or track.get('muted'): continue
                for other in track['clips']:
                    if (other.get('enabled', True) and other['asset_id'] == clip['asset_id']
                            and other['timeline_start'] < boundary + op['av_offset_frames'] + op['crossfade_frames']
                            and other['timeline_start'] + other['duration'] > boundary - op['av_offset_frames'] - op['crossfade_frames']):
                        fail('source audio is already routed on another audible track', 'duplicate_audio')
        if any(effect['type'] in {'audio_mix', 'transition'} and effect.get('enabled', True) for effect in plan.get('operations', [])):
            fail('explicit mixing/visual transitions conflict with split-edit routing', 'overlapping_audio_routing')
        if op['kind'] != 'crossfade':
            evidence = next((e for e in safety if isinstance(e, dict) and e.get('id') == op.get('evidence_id')), None)
            confidence = evidence.get('confidence') if evidence else None
            if not evidence or not (evidence.get('from_clip_id') == first['id'] and evidence.get('to_clip_id') == second['id']
                    and evidence.get('picture_boundary_frame') == boundary and type(confidence) in (int, float) and .8 <= confidence <= 1
                    and evidence.get('lips_visible_near_cut') is False and evidence.get(op['kind'] + '_safe') is True
                    and evidence.get('source_evidence_ids')):
                fail('L/J-cut requires reliable matching source evidence and no visible lip mismatch')
        offset = op['av_offset_frames'] * (1 if op['kind'] == 'l_cut' else -1 if op['kind'] == 'j_cut' else 0)
        crossfade = op['crossfade_frames']
        if crossfade > max(1, round(rate * Fraction(60, 1000))):
            fail('audio crossfade exceeds 60 ms', 'invalid_range')
        join = boundary + offset
        left_end = join + (crossfade + 1)//2
        right_start = join - crossfade//2
        left = routes.setdefault(first['id'], {**deepcopy(first), 'fades': []})
        right = routes.setdefault(second['id'], {**deepcopy(second), 'fades': []})
        left['duration'] = left_end - left['timeline_start']
        shift = right_start - right['timeline_start']
        right['source_in'] += shift
        right['duration'] -= shift
        right['timeline_start'] = right_start
        if crossfade:
            left['fades'].append(('out', right_start, crossfade))
            right['fades'].append(('in', right_start, crossfade))
    for clip_id, route in routes.items():
        if (route['source_in'] < 0 or route['timeline_start'] < 0 or route['duration'] <= 0
                or route['source_in'] + route['duration'] > assets[route['asset_id']]['duration_frames']):
            fail('audio source handle is outside available media', 'invalid_range')
        spans = sorted((start, start+length) for _, start, length in route['fades'])
        if any(start < route['timeline_start'] or end > route['timeline_start'] + route['duration'] for start, end in spans) or any(b[0] < a[1] for a,b in zip(spans, spans[1:])):
            fail('audio join regions overlap or exceed clip range', 'overlapping_audio_routing')
    return routes
