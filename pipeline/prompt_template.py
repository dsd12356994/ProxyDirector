"""Seedance 提示词模板。主体冻结为 20260922 紧凑取景批次验证过的文本（构图最好的一条成片所用），
仅数字槽位随相机方案与取景结果填充；LLM 只能改 emphasis_notes 补充句。"""

TEMPLATE = """Create one continuous photorealistic product video of the black wired mouse, its complete tangled cable and exactly one USB-A connector. The supplied gray reference VIDEO is the authority for CAMERA FRAMING, CAMERA ELEVATION, CAMERA DISTANCE, CAMERA PATH and the entire object arrangement. Match the screen-space position and scale of the entire assembly to the reference video at each corresponding time. Follow its compact composition without any additional crop, zoom, enlargement, dolly movement or reframing. Preserve its fixed lens and its complete continuous orbit. Keep the entire mouse, every cable loop and the full USB connector visible at EVERY moment. Maintain at least the reference video's approximately {margin_percent}% minimum edge clearance, including at side views and at the start and end. Do not reinterpret the portrait photographs as framing instructions.

The FOUR reference PHOTOS are appearance and identity references only, not instructions for framing or camera proximity. They show one unchanged object from different viewpoints. Use them to recover the mouse's black material, curved shell, two main buttons, one central scroll wheel, the small top button and two side buttons. Image 2 does not show the USB but the complete single USB shown in the other images must remain visible. Use the coarse video for the location and over-under ordering of the loops. Exactly one continuous unbranched cable connects the mouse to the single USB. No extra free ends, dangling cut wires, added clips, duplicate connectors, filled holes or added objects.{emphasis}

The mouse and every part of its cable remain completely stationary and rigid on a white studio tabletop. Camera makes exactly one smooth continuous 360-degree orbit over {duration} seconds at a constant elevated angle of about {elevation_degrees:.0f} degrees, constant distance and fixed lens, following the reference video. The four sides pass in order ({side_sequence}) and the camera returns to its initial view. No scene cuts, jumps, reversal, repeated partial arcs, object rotation, wire motion or object deformation. Preserve the reference video compact composition all the way through. White seamless background, soft lighting and faint contact shadow. No captions, hands or furniture. Silent video."""


def to_kling(text: str) -> str:
    """Kling 全能参考适配：素材指代改为 @id 语法（contents 里的 video_1/image_1..4），
    冻结文本的其余部分逐字保留。"""
    text = text.replace("The supplied gray reference VIDEO is the authority",
                        "The supplied gray reference video @video_1 is the authority")
    text = text.replace("The FOUR reference PHOTOS are appearance and identity references only",
                        "The four reference photos @image_1 @image_2 @image_3 @image_4 are "
                        "appearance and identity references only")
    text = text.replace("Use the coarse video for the location",
                        "Use the coarse video @video_1 for the location")
    return text


def render_prompt(report, duration: int, emphasis_notes: str = "", kling: bool = False,
                  style_block: str = "") -> str:
    emphasis = ""
    if emphasis_notes.strip():
        emphasis = " " + emphasis_notes.strip()
    text = TEMPLATE.format(
        margin_percent=round(min(report.min_margins_lbrt) * 100),
        duration=duration,
        elevation_degrees=report.plan.elevation_degrees,
        side_sequence=", then ".join(report.side_sequence),
        emphasis=emphasis,
    )
    if style_block.strip():                          # 提示词扩写：只附加，不改冻结正文
        text = text + "\n\n" + style_block.strip()
    return to_kling(text) if kling else text
