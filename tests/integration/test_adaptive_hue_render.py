"""Opt-in real MLT hue fixtures; no dependency installation or source mutation."""
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import unittest

from tests.helpers import valid_plan
from video_editing.mlt import write_mlt
from video_editing.plan import validate_plan
from video_editing.probe import fingerprint
from video_editing.render import render
from video_editing.silence import detect_silence
from video_editing.grading import inspect_hue_grades
from fractions import Fraction


@unittest.skipUnless(os.environ.get('VES_HUE_RENDER_DIR'), 'set VES_HUE_RENDER_DIR to a new job directory')
class HueRenderTests(unittest.TestCase):
    def test_red_and_blue_preserve_detail(self):
        root = Path(os.environ['VES_HUE_RENDER_DIR']).resolve()
        root.mkdir(parents=True,exist_ok=False)
        ffmpeg,melt = os.environ['VES_FFMPEG'],os.environ['VES_MELT']
        source = root/'source.mp4'
        def run(args):
            return subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=8',
             '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=8',
             '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac',str(source)])
        digest = fingerprint(source)
        def pixels(path,seconds):
            return run([ffmpeg,'-v','error','-ss',str(seconds),'-i',str(path),'-frames:v','1',
                        '-f','rawvideo','-pix_fmt','rgb24','-threads','1','-']).stdout
        baseline = pixels(source,5)
        def luminance(data): return [.2126*r+.7152*g+.0722*b for r,g,b in zip(data[0::3],data[1::3],data[2::3])]
        base_luma = luminance(baseline)
        report = {}
        for name,color,channel in [('red','#ff0000',0),('blue','#0000ff',2)]:
            plan = valid_plan(source)
            plan['profile'].update(width=320,height=180,frame_rate={'numerator':30,'denominator':1})
            plan['assets'][0]['duration_frames'] = 240
            plan['tracks'][0]['clips'][0]['duration'] = 240
            plan['operations'] = [{'id':name,'type':'color_grade','tail_seconds':5,'tint':color,'tint_strength':.75}]
            validated = validate_plan(plan,source=root/(name+'.json'))
            (root/(name+'.json')).write_text(json.dumps(validated.data,indent=2),encoding='utf-8')
            project,output = root/(name+'.mlt'),root/(name+'.mp4')
            write_mlt(validated,project)
            render(project,output,melt=melt,progress_stream=io.StringIO(),timeout=180,no_progress_timeout=90)
            data = pixels(output,5)
            self.assertEqual(len(data),320*180*3)
            means = [statistics.mean(data[index::3]) for index in range(3)]
            luma = luminance(data)
            detail = statistics.pstdev(luma)
            error = statistics.mean(abs(a-b) for a,b in zip(base_luma,luma))
            self.assertGreater(means[channel],max(means[i] for i in range(3) if i != channel)+30)
            self.assertGreater(detail,20)
            self.assertLess(error,35)
            # The unaffected first three seconds must remain effectively unchanged.
            before,original = pixels(output,1),pixels(source,1)
            self.assertLess(statistics.mean(abs(a-b) for a,b in zip(before,original)),8)
            run([ffmpeg,'-v','error','-ss','5','-i',str(output),'-frames:v','1','-threads','1',str(root/(name+'.png'))])
            report[name] = {'mean_rgb':means,'luminance_stddev':detail,'luminance_mae':error,'start_frame':90,'duration_frames':150}
            inspection = root/(name+'-inspection')
            inspection.mkdir()
            findings = inspect_hue_grades(validated.data,output,root/(name+'.json'),inspection,ffmpeg)
            self.assertEqual(len(findings),3)
            self.assertTrue(all(item['status'] == 'pass' for item in findings),findings)
            (inspection/'findings.json').write_text(json.dumps(findings,indent=2),encoding='utf-8')
        self.assertEqual(fingerprint(source),digest)
        (root/'inspection.json').write_text(json.dumps(report,indent=2),encoding='utf-8')

    def test_real_adaptive_audio(self):
        ffmpeg = os.environ['VES_FFMPEG']
        with tempfile.TemporaryDirectory(dir=Path(os.environ['VES_HUE_RENDER_DIR']).parent) as temp:
            thresholds = []
            for index,noise in enumerate((.001,.008)):
                path = Path(temp)/f'noise-{index}.wav'
                expression = f'aevalsrc=0.25*sin(2*PI*440*t)*(lt(t\\,1)+between(t\\,2\\,3))+{noise}*(2*random(0)-1):s=48000:d=4'
                subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i',expression,str(path)],check=True,capture_output=True)
                evidence = detect_silence(path,frame_rate=Fraction(30000,1001),ffmpeg=ffmpeg,duration_seconds='4')
                self.assertGreaterEqual(evidence['calibration']['confidence'],.6,evidence)
                self.assertTrue(evidence['intervals'],evidence)
                self.assertEqual(evidence['intervals'][-1]['end_frame'],120)
                thresholds.append(evidence['settings']['threshold_db'])
            self.assertGreater(thresholds[1],thresholds[0]+10)


if __name__ == '__main__': unittest.main()
