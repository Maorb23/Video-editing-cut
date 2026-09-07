from copy import deepcopy
from fractions import Fraction
import json
import xml.etree.ElementTree as ET

import pytest

from tests.helpers import valid_plan
from tests import test_planning as planning_fixtures
from tests.test_planning import FakeModel, draft
from video_editing.analysis import FrameAnalysisProvider
from video_editing.adaptive_silence import SilenceSettings
from video_editing.artifacts import validate_compiled_mlt
from video_editing.errors import PlanValidationError, VideoEditingError
from video_editing.filters import FILTERS
from video_editing.mlt import write_mlt
from video_editing.plan import validate_plan
from video_editing.planning import EditPlanner
from video_editing.planning.decisions import validate_decisions


def test_revision_context_and_rational_rate_are_preserved(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    analysis = planning_fixtures.PlanningTests().analysis(tmp_path, source)
    analysis.data["timeline_policy"]["frame_rate"] = {"numerator": 30000, "denominator": 1001}
    analysis.data["source"]["video"]["avg_frame_rate"] = "30000/1001"
    previous = draft(operations=[{"id": "bright", "type": "filter", "target": "c1", "name": "brightness", "properties": {"level": 0.35}}])
    revised = deepcopy(previous)
    revised["operations"][0]["properties"]["level"] = 0.5
    revised["unsupported"] = ["Pitch shifting is unsupported"]
    model = FakeModel([revised])
    result = EditPlanner(model).plan("Brighten more and shift pitch", analysis, plan_path=tmp_path / "plan.json", source_relative="source.mp4",
                                     previous_plan=previous, original_instruction="Brighten", allow_unsupported=True)
    context = json.loads(model.calls[0]["input_text"])
    assert context["previous_plan"] == previous
    assert context["original_instruction"] == "Brighten"
    assert result.plan.profile["frame_rate"] == {"numerator": 30000, "denominator": 1001}
    assert result.decision_log["unsupported"] == revised["unsupported"]
    assert FrameAnalysisProvider(silence_settings=SilenceSettings()).frame_rate is None


def test_public_logs_reject_unknown_evidence_private_fields_and_invalid_confidence():
    log = {"observations": [], "decisions": [], "unsupported": [], "assumptions": []}
    assert validate_decisions(log, set()) == log
    with pytest.raises(VideoEditingError):
        validate_decisions({**log, "chain_of_thought": "private"}, set())
    for confidence, evidence in ((float("nan"), []), (1.2, []), (0.8, ["unknown.png"])):
        observation = {"type": "visual", "description": "subject", "confidence": confidence, "evidence": evidence}
        with pytest.raises(VideoEditingError):
            validate_decisions({**log, "observations": [observation]}, set())


def test_plan_cannot_drop_source_audio(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    analysis = planning_fixtures.PlanningTests().analysis(tmp_path, source)
    analysis.data["source"]["audio"] = {"sample_rate": 48000, "channels": 2}
    muted = draft()
    muted["tracks"][0]["muted"] = True
    with pytest.raises(VideoEditingError, match="source audio"):
        EditPlanner(FakeModel([muted])).plan("Brighten", analysis, plan_path=tmp_path / "plan.json", source_relative="source.mp4")


def test_geometry_overlap_rejected_but_adjacent_intervals_allowed(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    plan = valid_plan(source)
    plan["operations"] = [
        {"id": "a", "type": "transform", "target": "c1", "start": 0, "duration": 20, "geometry": "10%/10%:80%x80%"},
        {"id": "b", "type": "transform", "target": "c1", "start": 19, "duration": 10, "geometry": "10%/10%:80%x80%"},
    ]
    with pytest.raises(PlanValidationError) as error:
        validate_plan(plan, source=tmp_path / "plan.json")
    assert any(issue.code == "conflicting_transform" for issue in error.value.issues)
    plan["operations"][1]["start"] = 20
    validate_plan(plan, source=tmp_path / "plan.json")


def test_brightness_semantics_and_compiled_artifact_tampering(tmp_path):
    assert FILTERS["brightness"].transform({"level": 0.35}) == {"level": "1.35"}
    assert FILTERS["brightness"].transform({"level": -0.35}) == {"level": "0.65"}
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    data = valid_plan(source)
    data["profile"]["frame_rate"] = {"numerator": 30000, "denominator": 1001}
    data["operations"] = [{"id": "bright", "type": "filter", "target": "c1", "name": "brightness", "properties": {"level": 0.35}}]
    plan = validate_plan(data, source=tmp_path / "plan.json")
    mlt = tmp_path / "project.mlt"
    write_mlt(plan, mlt)
    before = mlt.read_bytes()
    validate_compiled_mlt(mlt, plan, tmp_path)
    with pytest.raises(VideoEditingError):
        write_mlt(plan, mlt)
    assert mlt.read_bytes() == before
    tree = ET.parse(mlt)
    tree.getroot().find("profile").set("frame_rate_num", "30")
    tree.write(mlt, encoding="utf-8")
    with pytest.raises(VideoEditingError, match="does not match"):
        validate_compiled_mlt(mlt, plan, tmp_path)


def test_audio_fade_converts_amplitude_to_mlt_decibels(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    data = valid_plan(source)
    data["operations"] = [{"id": "fade", "type": "fade_audio", "target": "c1", "direction": "out", "start": 80, "duration": 20}]
    plan = validate_plan(data, source=tmp_path / "plan.json")
    mlt = tmp_path / "project.mlt"
    write_mlt(plan, mlt)
    root = ET.parse(mlt)
    assert root.find("./producer/filter[@id='ves_filter_fade']/property[@name='level']").text == "0=0;19=-90"
    assert root.find("./tractor/multitrack/track[@producer='ves_playlist_0_v1']").get("hide") is None
    assert root.find("./tractor/transition[@id='ves_audio_mix_track_1']") is not None
