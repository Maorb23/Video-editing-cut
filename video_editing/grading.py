"""Resolve bounded tail-color intent against the final deterministic timeline."""
from copy import deepcopy
from fractions import Fraction
import colorsys
import statistics

from .errors import VideoEditingError
from .operations import resolve_timeline


def resolve_tail_grades(plan: dict) -> dict:
    output = deepcopy(plan)
    tracks = resolve_timeline(plan)
    end = max((c['timeline_start']+c['duration'] for t in tracks for c in t['clips'] if c.get('enabled',True)), default=0)
    rate = Fraction(**plan['profile']['frame_rate'])
    operations = []
    for op in output['operations']:
        if 'tail_seconds' not in op:
            operations.append(op); continue
        duration = round(Fraction(str(op['tail_seconds']))*rate)
        if duration <= 0 or duration > end:
            raise VideoEditingError('tail grade duration must fit the final timeline', code='invalid_range')
        count = 0
        for track in tracks:
            if track['kind'] != 'video' or track.get('hidden'): continue
            for clip in track['clips']:
                if not clip.get('enabled',True): continue
                a,b = max(end-duration,clip['timeline_start']), min(end,clip['timeline_start']+clip['duration'])
                if a < b:
                    derived = {k:v for k,v in op.items() if k != 'tail_seconds'}
                    derived.update(id=f"{op['id']}__tail_{count:03d}",target=clip['id'],start=a-clip['timeline_start'],duration=b-a)
                    operations.append(derived)
                    count += 1
        if not count:
            raise VideoEditingError('tail grade has no visible target', code='missing_target')
    output['operations'] = operations
    return output


def hue_metrics(pixels: bytes, tint: str) -> dict:
    """Measure hue agreement and retained spatial luminance variation in RGB24."""
    target = tuple(int(tint[i:i+2],16)/255 for i in (1,3,5))
    hue, saturation, _ = colorsys.rgb_to_hsv(*target)
    errors, luma = [], []
    for r,g,b in zip(pixels[0::3],pixels[1::3],pixels[2::3]):
        actual,sat,_ = colorsys.rgb_to_hsv(r/255,g/255,b/255)
        luma.append(.2126*r+.7152*g+.0722*b)
        if sat > .15 and max(r,g,b)-min(r,g,b) > 20:
            delta = abs(actual-hue)
            errors.append(min(delta,1-delta)*360)
    return {'hue_error_degrees': statistics.median(errors) if errors else None,
            'chromatic_fraction':len(errors)/max(1,len(luma)),
            'luminance_stddev':statistics.pstdev(luma) if luma else 0,
            'luminance_mean':statistics.mean(luma) if luma else 0,
            'target_saturation':saturation}


def inspect_hue_grades(plan: dict, video, plan_path, output_dir, ffmpeg: str) -> list[dict]:
    """Persist bounded frame samples and reject missing hue/detail where measurable."""
    from .process import run_checked
    tracks = resolve_timeline(plan)
    clips = {c['id']:c for t in tracks for c in t['clips']}
    assets = {a['id']:a for a in plan['assets']}
    rate = Fraction(**plan['profile']['frame_rate'])
    findings = []
    def sample(path, seconds, destination):
        run_checked([ffmpeg,'-v','error','-ss',str(float(seconds)),'-i',str(path),'-frames:v','1',
                     '-vf','scale=160:90','-pix_fmt','rgb24','-threads','1',str(destination)])
        header = destination.read_bytes().split(b'\n',3)
        if len(header) != 4 or header[:3] != [b'P6',b'160 90',b'255'] or len(header[3]) != 160*90*3:
            raise VideoEditingError('invalid hue inspection frame',code='inspection_failed')
        return header[3]
    for op in plan.get('operations',[]):
        if op['type'] != 'color_grade' or not op.get('enabled',True) or op.get('tint_strength',0) < .5: continue
        clip = clips[op['target']]
        start = op.get('start',0)
        duration = op.get('duration',clip['duration']-start)
        # Three samples cover the start, middle, and final selected frame.
        for local in sorted({start,start+duration//2,start+duration-1}):
            frame = clip['timeline_start']+local
            stem = f'hue-{len(findings):04d}'
            rendered_path,source_path = output_dir/(stem+'-render.ppm'),output_dir/(stem+'-source.ppm')
            pixels = sample(video,Fraction(frame,1)/rate,rendered_path)
            source = plan_path.resolve().parent/assets[clip['asset_id']]['path']
            original = sample(source,Fraction(clip['source_in']+local,1)/rate,source_path)
            measured = hue_metrics(pixels,op['tint'])
            original_metrics = hue_metrics(original,op['tint'])
            chromatic_target = measured['target_saturation'] > .2 and op.get('saturation',1) > .5
            enough_light = original_metrics['luminance_stddev'] > 5
            visible_light = 10 < original_metrics['luminance_mean'] < 245
            hue_failed = chromatic_target and visible_light and (measured['hue_error_degrees'] is None or measured['hue_error_degrees'] > 30)
            flat = enough_light and measured['luminance_stddev'] < max(2,original_metrics['luminance_stddev']*.1)
            findings.append({'operation_id':op['id'],'timeline_frame':frame,'code':'hue_grade_conformance',
                'status':'fail' if hue_failed or flat else 'pass','metrics':measured,'source_metrics':original_metrics,
                'rendered_frame_path':str(rendered_path.resolve()),'source_frame_path':str(source_path.resolve())})
    return findings
