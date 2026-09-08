from copy import deepcopy
from dataclasses import asdict
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from tests.helpers import valid_plan
from tests.test_planning import FakeModel, draft
from tests import test_planning
from video_editing.adaptive_silence import (SilenceSettings, calibrate_noise,
                                            detect_quiet_intervals, requested_speech_padding_seconds,
                                            silence_policy, silence_settings_for_instruction)
from video_editing.audio_transitions import audio_routes
from video_editing.errors import PlanValidationError, VideoEditingError
from video_editing.mlt import compile_mlt
from video_editing.plan import validate_plan
from video_editing.render import render, verify_filter_services
from video_editing.planning import EditPlanner
from video_editing.silence import parse_silence_output
from video_editing.silence_edits import (PREFLIGHT_REQUIRED_MESSAGE, apply_silence_decisions,
                                         require_silence_preflight, silence_review_markdown)


class AdaptiveSilenceTests(unittest.TestCase):
    def test_phone_video_defaults(self):
        settings = SilenceSettings()
        self.assertEqual(settings.minimum_silence_seconds,.25)
        self.assertEqual(settings.speech_padding_seconds,.12)

    def test_instruction_padding_is_explicit_and_not_pause_duration(self):
        instruction = ('Remove pauses longer than 0.5 seconds, keep natural speech rhythm, '
                       'preserve 0.12 seconds of padding')
        self.assertEqual(requested_speech_padding_seconds(instruction), .12)
        self.assertEqual(silence_settings_for_instruction(SilenceSettings(), instruction).speech_padding_seconds, .12)
        self.assertIsNone(requested_speech_padding_seconds('Remove pauses longer than 0.5 seconds'))
        self.assertEqual(requested_speech_padding_seconds('use 500 ms speech padding'), .5)
        self.assertEqual(requested_speech_padding_seconds('padding: .2 sec'), .2)

    def test_instruction_padding_precedence_inheritance_and_bounds(self):
        base = SilenceSettings(speech_padding_seconds=.12)
        self.assertEqual(silence_settings_for_instruction(
            base, 'make the final seconds red', inherited_padding_seconds=.4,
        ).speech_padding_seconds, .4)
        self.assertEqual(silence_settings_for_instruction(
            base, 'change padding to 0.2 seconds', inherited_padding_seconds=.4,
        ).speech_padding_seconds, .2)
        for instruction in ('padding 0.2 seconds and padding 0.3 seconds', 'use 1.1 seconds of padding'):
            with self.subTest(instruction=instruction), self.assertRaises(VideoEditingError):
                silence_settings_for_instruction(base, instruction)

    def test_configured_padding_controls_retained_pause_frames(self):
        candidate = {'id':'pause','start_frame':0,'end_frame':60}
        policy = silence_policy(candidate, Fraction(30), SilenceSettings(speech_padding_seconds=.5))
        self.assertEqual(policy['action'], 'shorten')
        self.assertEqual(policy['retained_frames'], 30)

    def test_noise_calibration_and_clamping(self):
        for floor, expected in [(-58,-50),(-42,-34),(-85,-60),(-30,-30)]:
            with self.subTest(floor=floor):
                result = calibrate_noise([floor]*20+[-10]*80,SilenceSettings())
                self.assertEqual(result['noise_floor_dbfs'],floor)
                self.assertEqual(result['threshold_db'],expected)
                self.assertFalse(result['fallback'])
        result = calibrate_noise([-100]+[-42]*19+[-10]*80,SilenceSettings())
        self.assertEqual(result['noise_floor_dbfs'],-42)

    def test_low_confidence_fallback(self):
        for values in ([],[-30]*100,[-60,-10],[-float('inf')]*100):
            result = calibrate_noise(values,SilenceSettings())
            self.assertEqual(result['threshold_db'],-50)
            self.assertTrue(result['fallback'])

    def test_rms_detection_is_not_broken_by_phone_noise_peaks_inside_windows(self):
        windows = [(Fraction(index,20), level) for index,level in enumerate(
            [-18]*4+[-43,-42,-44,-41,-43,-42,-18]*1)]
        self.assertEqual(detect_quiet_intervals(
            windows,threshold_db=-40,window_seconds=.05,minimum_seconds=.25,
            duration_seconds=Fraction(11,20)),[(Fraction(1,5),Fraction(1,2))])

    def test_eof_and_rational_frames_and_short_regions(self):
        result = parse_silence_output('silence_start: 1.001\nsilence_end: 1.701\nsilence_start: 2.002',
            source=Path('source'),frame_rate=Fraction(30000,1001),threshold_db=-42,minimum_seconds=.5,duration_seconds='3.003')
        self.assertEqual([(i['start_frame'],i['end_frame']) for i in result['intervals']],[(30,51),(60,90)])
        short = parse_silence_output('silence_start: 0\nsilence_end: .1',source=Path('s'),frame_rate=Fraction(30),threshold_db=-42,minimum_seconds=.5)
        self.assertEqual(short['intervals'],[])

    def test_eof_candidate_is_clamped_to_editable_video_frames(self):
        result = parse_silence_output(
            'silence_start: 4.9\nsilence_end: 5.2', source=Path('source'), frame_rate=Fraction(30),
            threshold_db=-42, minimum_seconds=.25, duration_seconds='5.2', maximum_frames=150,
        )
        self.assertEqual([(item['start_frame'], item['end_frame']) for item in result['intervals']], [(147, 150)])

    def test_duration_policy_and_context(self):
        for frames,action in [(9,'keep'),(18,'shorten'),(30,'shorten')]:
            decision = silence_policy({'start_frame':0,'end_frame':frames},Fraction(30))
            self.assertEqual(decision['action'],action)
            self.assertEqual(decision['transition'],'synchronized')
        candidate = {'start_frame':0,'end_frame':30,'context':{'confidence':.95,'evidence_ids':['transcript:1'],'emphasis_score':'high'}}
        self.assertEqual(silence_policy(candidate,Fraction(30))['retained_frames'],18)
        candidate['context'].update(emphasis_score='low',non_speech=True)
        self.assertEqual(silence_policy(candidate,Fraction(30))['action'],'remove')
        candidate['context'].update(lips_visible_near_cut=False,l_cut_safe=True,visual_discontinuity='low',room_tone_difference='low')
        self.assertEqual(silence_policy(candidate,Fraction(30))['transition'],'l_cut')
        candidate['context'].pop('evidence_ids')
        self.assertEqual(silence_policy(candidate,Fraction(30))['transition'],'synchronized')


class TypedEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root/'source.mp4'
        self.media.write_bytes(b'immutable')
        self.plan = valid_plan(self.media)
        self.plan['profile']['frame_rate'] = {'numerator':30,'denominator':1}
        self.plan['assets'][0]['probe'] = {'audio':{'sample_rate':48000}}

    def join(self, kind):
        self.plan = valid_plan(self.media)
        self.plan['profile']['frame_rate'] = {'numerator':30,'denominator':1}
        self.plan['assets'][0]['probe'] = {'audio':{'sample_rate':48000}}
        self.plan['tracks'][0]['clips'] = [
            {'id':'a','asset_id':'a','timeline_start':0,'source_in':10,'duration':60},
            {'id':'b','asset_id':'a','timeline_start':60,'source_in':100,'duration':60}]
        self.plan['operations'] = [{'id':'join','type':'audio_transition','kind':kind,'from_clip_id':'a','to_clip_id':'b',
            'picture_boundary_frame':60,'av_offset_frames':0 if kind == 'crossfade' else 4,'crossfade_frames':2,'evidence_id':'safe'}]
        self.plan['analysis'] = {'transition_safety':[{'id':'safe','from_clip_id':'a','to_clip_id':'b','picture_boundary_frame':60,
            'confidence':.95,'lips_visible_near_cut':False,'l_cut_safe':True,'j_cut_safe':True,'source_evidence_ids':['video:59']}]}

    def test_l_j_crossfade_routes_and_no_duplicate_audio(self):
        for kind,start,end in [('l_cut',63,65),('j_cut',55,57),('crossfade',59,61)]:
            self.join(kind)
            validated = validate_plan(self.plan,source=self.root/'plan.json')
            routes = audio_routes(validated.data,list(validated.resolved_tracks))
            self.assertEqual(routes['a']['duration'],end)
            self.assertEqual(routes['b']['timeline_start'],start)
            self.assertEqual(routes['b']['source_in'],100+start-60)
            xml = compile_mlt(validated,self.root/'out.mlt').getroot()
            muted = [p for p in xml.findall('producer') if p.find("./property[@name='audio_index']") is not None]
            self.assertEqual(len(muted),2)
            audio_tracks = [t for t in xml.findall('./tractor/multitrack/track') if t.get('hide') == 'video']
            self.assertEqual(len(audio_tracks),2)
            self.assertEqual(len(xml.findall("./producer/filter/property[@name='level']")),2)

    def test_unsafe_and_handle_and_duplicate_rejection(self):
        for invalid in ('handles','lips','missing','adjacency','duplicate','offset','routing'):
            self.join('j_cut')
            if invalid == 'handles': self.plan['tracks'][0]['clips'][1]['source_in'] = 0
            if invalid == 'lips': self.plan['analysis']['transition_safety'][0]['lips_visible_near_cut'] = True
            if invalid == 'missing': self.plan['analysis'] = {}
            if invalid == 'adjacency': self.plan['operations'][0]['picture_boundary_frame'] = 59
            if invalid == 'offset': self.plan['operations'][0]['av_offset_frames'] = 7
            if invalid == 'duplicate':
                self.plan['tracks'].append({'id':'audio','kind':'audio','clips':[dict(self.plan['tracks'][0]['clips'][1],id='duplicate')]})
            if invalid == 'routing': self.plan['operations'].append(dict(self.plan['operations'][0],id='other'))
            with self.subTest(invalid=invalid),self.assertRaises(PlanValidationError): validate_plan(self.plan,check_files=False)

    def test_crossfade_intermediate_amplitudes_do_not_dip(self):
        self.join('crossfade')
        self.plan['profile']['frame_rate'] = {'numerator':120,'denominator':1}
        self.plan['operations'][0]['crossfade_frames'] = 5
        xml = compile_mlt(validate_plan(self.plan,source=self.root/'plan.json'),self.root/'out.mlt').getroot()
        levels = [p.text for p in xml.findall("./producer/filter/property[@name='level']")]
        self.assertEqual(len(levels),2)
        self.assertTrue(all('2=-6.0206' in value for value in levels),levels)

    def evidence(self):
        settings = SilenceSettings()
        fingerprint = self.plan['assets'][0]['fingerprint']
        candidate = {'id':'pause','candidate_id':'pause','start_frame':60,'end_frame':90,
            'duration_frames':30,'duration_seconds':'1','confidence':.9,'threshold_db':-42,
            'source_fingerprint':fingerprint,
            'contextual_evidence':{'status':'unavailable','confidence':0.,'evidence_ids':[]},
            'evidence_id':'analysis/silence.json#pause'}
        candidate['suggestion'] = silence_policy(candidate,Fraction(30))
        return {'version':'1.0','status':'complete','asset_id':'a','source_fingerprint':fingerprint,
            'frame_rate':self.plan['profile']['frame_rate'],'analyzed_duration_seconds':'10',
            'analyzed_duration_frames':300,'intervals':[candidate],
            'settings':{**asdict(settings),'threshold_db':-42,'selected_threshold_db':-42},
            'detector':{'name':'rms_window_threshold/v1','scope':'full_source','threshold_db':-42,
                        'minimum_silence_seconds':settings.minimum_silence_seconds},
            'calibration':{'confidence':.9},'evidence_id':'analysis/silence.json'}

    def attach_preflight(self, analysis, evidence):
        evidence = deepcopy(evidence)
        evidence['asset_id'] = 'source'
        evidence['source_fingerprint'] = analysis.data['source']['fingerprint']
        evidence['analyzed_duration_seconds'] = str(Fraction(analysis.data['source']['duration_frames'],30))
        evidence['analyzed_duration_frames'] = analysis.data['source']['duration_frames']
        for candidate in evidence['intervals']:
            candidate['source_fingerprint'] = evidence['source_fingerprint']
        settings = {key:value for key,value in evidence['settings'].items() if key in SilenceSettings.__dataclass_fields__}
        analysis.data['source']['duration_seconds'] = evidence['analyzed_duration_seconds']
        analysis.data['source']['audio'] = {'sample_rate':48000,'channels':2}
        analysis.data['analysis_configuration'] = {'silence':settings}
        analysis.data['audio_evidence'] = {'silence':evidence,'evidence_id':'analysis/silence.json'}
        analysis.data['preflight'] = {'silence':{'status':'complete','evidence_id':'analysis/silence.json',
            'minimum_silence_seconds':settings['minimum_silence_seconds'],
            'speech_padding_seconds':settings['speech_padding_seconds'],
            'analyzed_duration_seconds':evidence['analyzed_duration_seconds']}}
        evidence_path = analysis.path.parent/'analysis'/'silence.json'
        evidence_path.parent.mkdir(exist_ok=True)
        evidence_path.write_text(json.dumps(evidence),encoding='utf-8')
        return evidence

    def test_policy_ripple_preserves_tracks_and_effects(self):
        self.plan['tracks'][0]['clips'][0]['duration'] = 240
        self.plan['tracks'].append({'id':'audio','kind':'audio','muted':True,'clips':[dict(self.plan['tracks'][0]['clips'][0],id='aud')]})
        self.plan['operations'] = [{'id':'grade','type':'color_grade','target':'c1','start':150,'duration':90,'tint':'#0000ff','tint_strength':.75}]
        edited = apply_silence_decisions(self.plan,self.evidence(),[{'asset_id':'a','candidate_id':'pause','action':'shorten'}])
        validated = validate_plan(edited,source=self.root/'plan.json')
        self.assertEqual(sum(c['duration'] for c in validated.resolved_tracks[0]['clips']),218)
        for v,a in zip(*[t['clips'] for t in validated.resolved_tracks]):
            self.assertEqual((v['source_in'],v['duration'],v['timeline_start']),(a['source_in'],a['duration'],a['timeline_start']))
        self.assertEqual(edited['operations'][0]['start'],64)
        self.assertEqual(self.media.read_bytes(),b'immutable')
        self.assertIn('## Detected silences',silence_review_markdown(self.evidence(),Fraction(30)))
        with self.assertRaises(VideoEditingError):
            apply_silence_decisions(self.plan,self.evidence(),[{'asset_id':'a','candidate_id':'pause','action':'remove'}])

    def test_tail_grade_and_bounds(self):
        self.plan['tracks'][0]['clips'][0]['duration'] = 240
        for color in ('#ff0000','#0000ff','#00ff00','#ff9900','#7829ab'):
            self.plan['operations'] = [{'id':'tail','type':'color_grade','tail_seconds':5,'tint':color,'tint_strength':.75}]
            validated = validate_plan(self.plan,source=self.root/'plan.json')
            grade = validated.data['operations'][0]
            self.assertEqual((grade['start'],grade['duration']),(90,150))
            xml = compile_mlt(validated,self.root/'out.mlt').getroot()
            self.assertEqual(xml.find("./producer/filter/property[@name='mlt_service']").text,'avfilter.colorize')
        for strength in (-.1,1.1,float('nan'),True):
            self.plan['operations'][0]['tint_strength'] = strength
            with self.assertRaises(PlanValidationError): validate_plan(self.plan,check_files=False)

    def test_hue_pin_rejects_unverified_runtime(self):
        self.plan['operations'] = [{'id':'hue','type':'color_grade','target':'c1','tint':'#ff9900','tint_strength':.75}]
        project = self.root/'pin.mlt'
        compile_mlt(validate_plan(self.plan,source=self.root/'plan.json'),project).write(project,encoding='utf-8')
        runner = Mock()
        runner.run.return_value = Mock(returncode=0,stdout='version: Lavfi7.0\n',stderr='')
        with self.assertRaises(VideoEditingError) as caught:
            render(project,self.root/'out.mp4',melt='melt',supervisor=runner)
        self.assertEqual(caught.exception.code,'unsupported_filter_version')
        self.assertFalse((self.root/'out.mp4').exists())

    def test_hue_pin_accepts_worker_lavfi_runtime(self):
        self.plan['operations'] = [{'id':'hue','type':'color_grade','target':'c1','tint':'#ff9900','tint_strength':.75}]
        project = self.root/'pin-compatible.mlt'
        compile_mlt(validate_plan(self.plan,source=self.root/'plan.json'),project).write(project,encoding='utf-8')
        runner = Mock()
        runner.run.return_value = Mock(returncode=0,stdout='version: Lavfi8.44.100\n',stderr='')
        verify_filter_services(project,'melt',runner)

    def test_unsafe_policy_choice_gets_bounded_repair(self):
        analysis = test_planning.PlanningTests().analysis(self.root,self.media,duration=240)
        evidence = self.attach_preflight(analysis,self.evidence())
        bad = draft(duration=240)
        bad['silence_decisions'] = [{'asset_id':'source','candidate_id':'pause','action':'remove'}]
        good = deepcopy(bad)
        good['silence_decisions'][0]['action'] = 'shorten'
        result = EditPlanner(FakeModel([bad,good])).plan('Remove long pauses',analysis,plan_path=self.root/'plan.json',source_relative=self.media.name)
        self.assertEqual(len(result.attempts),2)
        self.assertEqual(result.plan.data['analysis']['silence_decisions'][0]['action'],'shorten')

    def test_planner_uses_persisted_policy_then_resolves_tail(self):
        analysis = test_planning.PlanningTests().analysis(self.root,self.media,duration=240)
        evidence = self.attach_preflight(analysis,self.evidence())
        response = draft(duration=240,operations=[{'id':'blue','type':'color_grade','tail_seconds':5,'tint':'#0000ff','tint_strength':.75}])
        response['silence_decisions'] = [{'asset_id':'source','candidate_id':'pause','action':'shorten'}]
        result = EditPlanner(FakeModel([response])).plan('Remove long pauses and make the last 5 seconds blue',analysis,plan_path=self.root/'plan.json',source_relative=self.media.name)
        grade = result.plan.data['operations'][0]
        self.assertEqual(grade['duration'],150)
        self.assertEqual(result.plan.data['analysis']['silence_decisions'][0]['action'],'shorten')
        self.assertEqual(result.decision_log['decisions'][-1]['operation'],'pause: shorten (synchronized)')

    def test_preflight_rejects_missing_stale_partial_and_mismatched_evidence(self):
        analysis = test_planning.PlanningTests().analysis(self.root,self.media,duration=240)
        evidence = self.attach_preflight(analysis,self.evidence())
        self.assertIs(require_silence_preflight(analysis),evidence)
        cases = {
            'missing': lambda data: data.pop('audio_evidence'),
            'stale': lambda data: data['audio_evidence']['silence'].__setitem__('source_fingerprint','sha256:stale'),
            'partial': lambda data: data['audio_evidence']['silence'].__setitem__('status','partial'),
            'minimum': lambda data: data['analysis_configuration']['silence'].__setitem__('minimum_silence_seconds',.5),
            'padding': lambda data: data['analysis_configuration']['silence'].__setitem__('speech_padding_seconds',.5),
        }
        for name, mutate in cases.items():
            broken = deepcopy(analysis.data)
            mutate(broken)
            with self.subTest(name=name),self.assertRaisesRegex(VideoEditingError,PREFLIGHT_REQUIRED_MESSAGE):
                require_silence_preflight(broken,evidence_path=self.root/'analysis'/'silence.json')

    def test_pause_planning_is_blocked_before_model_call_without_preflight(self):
        analysis = test_planning.PlanningTests().analysis(self.root,self.media,duration=240)
        model = FakeModel([draft(duration=240)])
        with self.assertRaisesRegex(VideoEditingError,PREFLIGHT_REQUIRED_MESSAGE):
            EditPlanner(model).plan('Remove pauses',analysis,plan_path=self.root/'plan.json',
                                    source_relative=self.media.name)
        self.assertEqual(model.calls,[])

    def test_policy_l_cut_requires_context_and_handles(self):
        self.plan['tracks'][0]['clips'][0]['duration'] = 200
        evidence = self.evidence()
        evidence['intervals'][0]['contextual_evidence'] = {'confidence':.95,'evidence_ids':['video:60'],
            'emphasis_score':'low','lips_visible_near_cut':False,'visual_discontinuity':'low',
            'room_tone_difference':'low','l_cut_safe':True}
        result = apply_silence_decisions(self.plan,evidence,[{'candidate_id':'pause','asset_id':'a','action':'shorten'}])
        self.assertEqual(result['operations'][-1]['kind'],'l_cut')
        validate_plan(result,source=self.root/'plan.json')


if __name__ == '__main__': unittest.main()
