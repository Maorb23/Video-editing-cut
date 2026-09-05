"""Provider-neutral source analysis interfaces."""

from .base import AnalysisArtifact, AnalysisProvider
from .frames import FrameAnalysisProvider, project_duration_frames, sample_project_frames

__all__ = ["AnalysisArtifact", "AnalysisProvider", "FrameAnalysisProvider", "project_duration_frames", "sample_project_frames"]
