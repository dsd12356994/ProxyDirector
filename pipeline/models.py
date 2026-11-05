"""LLM 决策与机器检查的数据结构。"""
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


class ShotPlan(BaseModel):
    """camera plan：LLM 唯一可改的相机参数。半径按归一化后（最长水平尺寸=3）的尺度。"""
    elevation_degrees: float = Field(50.0, ge=5.0, le=85.0, description="相机俯视角，度")
    radius: float = Field(6.0, ge=3.0, le=12.0, description="环绕半径（模型水平尺寸归一为3后）")
    start_degrees: float = Field(0.0, ge=-360.0, le=360.0, description="起始方位角")
    direction: Literal["cw", "ccw"] = Field("cw", description="环绕方向")
    margin: float = Field(0.05, ge=0.03, le=0.15, description="四面安全边距（画面比例）")


class FramingReport(BaseModel):
    """取景机器检查结果，回给 LLM 决策。"""
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
            return (f"取景检查通过：垂直FOV {self.fov_vertical_degrees:.2f}°，"
                    f"最小边距(左/下/右/上) {['%.1f%%' % (m*100) for m in self.min_margins_lbrt]}，"
                    f"侧面顺序 {self.side_sequence}，{self.frames} 帧已渲染。")
        return "取景检查未通过：" + ";".join(self.violations[:5])


class SeedanceRequest(BaseModel):
    """付费提交前冻结的请求要素。"""
    quality: Literal["480p", "720p"]
    prompt_path: str
    reference_video_sha256: str
    estimated_credits: int
    estimated_usd: float
