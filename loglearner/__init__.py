"""LogLearner — mine agent session logs for training examples."""

from .pipeline import Pipeline, PipelineContext, StepProtocol, PipelineError
from .steps import (
    ScanStep,
    AdapterDispatchStep,
    DetectGapsStep,
    ComputeStateVectorStep,
    MeasureRuleImpactStep,
    HarvestContextStep,
    SynthesizeStep,
    VerifyStep,
    MintArmsStep,
    WriteIsolatesStep,
    WriteToHindsightStep,
    WriteOutputsStep,
)


def build_default_pipeline(
    max_sessions: int = 20,
    adapter_sample_lines: int = 200,
) -> Pipeline:
    """Compose the default v2 pipeline (scan → adapters → gaps → context → synth → write)."""
    return (
        Pipeline()
        .then(ScanStep(max_sessions=max_sessions))
        .then(AdapterDispatchStep(sample_lines=adapter_sample_lines))
        .then(DetectGapsStep())
        .then(ComputeStateVectorStep())
        .then(MeasureRuleImpactStep())
        .then(HarvestContextStep())
        .then(SynthesizeStep())
        .then(VerifyStep())
        .then(MintArmsStep())
        .then(WriteIsolatesStep())
        .then(WriteToHindsightStep())
        .then(WriteOutputsStep())
    )


__all__ = [
    "Pipeline",
    "PipelineContext",
    "StepProtocol",
    "PipelineError",
    "ScanStep",
    "AdapterDispatchStep",
    "DetectGapsStep",
    "ComputeStateVectorStep",
    "MeasureRuleImpactStep",
    "HarvestContextStep",
    "SynthesizeStep",
    "VerifyStep",
    "MintArmsStep",
    "WriteIsolatesStep",
    "WriteToHindsightStep",
    "WriteOutputsStep",
    "build_default_pipeline",
]
