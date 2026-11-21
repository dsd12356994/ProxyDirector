"""LLM 决策与机器检查的数据结构。"""
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


class ShotPlan(BaseModel):
    """Camera controls for the director. Radius uses the normalized model scale."""
    elevation_degrees: float = Field(50.0, ge=5.0, le=85.0, description="Camera elevation in degrees")
    radius: float = Field(6.0, ge=3.0, le=12.0, description="Orbit radius in scene units, with the longest horizontal dimension normalized to 3")
    start_degrees: float = Field(0.0, ge=-360.0, le=360.0, description="Starting azimuth in degrees")
    direction: Literal["cw", "ccw"] = Field("cw", description="Orbit direction")
    margin: float = Field(0.05, ge=0.03, le=0.15, description="Required edge clearance as a fraction of the frame")


class FramingReport(BaseModel):
    """Measured framing data returned to the director."""
    passed: bool
    plan: ShotPlan
    fov_vertical_degrees: float
    lens_mm: float
    min_margins_lbrt: list[float]          # 整圈左/下/右/上最小边距
    side_sequence: list[str]               # 环绕依次呈现的面
    vertices: int
    frames: int
    violations: list[str] = []             # 未通过时的具体原因（截断）
    video_path: Optional[str] = None
    render_seconds: Optional[float] = None

    def summary(self) -> str:
        if self.passed:
            return (f"Framing passed. Vertical field of view {self.fov_vertical_degrees:.2f}°. "
                    f"Minimum clearance (left, bottom, right, top): {', '.join(f'{m:.2%}' for m in self.min_margins_lbrt)}. "
                    f"View order: {' → '.join(self.side_sequence)}. {self.frames} frames rendered.")
        return "Framing failed. " + " ".join(self.violations[:5])


class SeedanceRequest(BaseModel):
    """付费提交前冻结的请求要素。"""
    quality: Literal["480p", "720p"]
    prompt_path: str
    reference_video_sha256: str
    estimated_credits: int
    estimated_usd: float
