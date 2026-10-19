"""Synthetic, redistributable DXF. Never a customer drawing or acceptance baseline."""

from pathlib import Path
import ezdxf


def create_demo(path: Path):
    doc = ezdxf.new("R2018")
    doc.units = 4
    m = doc.modelspace()

    def line(a, b):
        m.add_line(a, b)

    def text(value, x, y, height=2.8):
        return m.add_text(value, dxfattribs={"insert": (x, y), "height": height})

    def rect(a, b):
        x, y = a
        xx, yy = b
        m.add_lwpolyline([(x, y), (xx, y), (xx, yy), (x, yy)], close=True)

    rect((0, 0), (420, 297))
    rect((10, 10), (410, 287))
    text("建筑设计总说明 · 砌体工程", 25, 268, 6)
    text("SYNTHETIC DRAWING  /  源图证据演示", 25, 255, 2.7)
    line((25, 248), (395, 248))
    text("01  墙体材料说明", 25, 234, 4)
    rows = [
        "地上内墙采用B06级A3.5蒸压加气混凝土砌块，100或200厚，Ma5.0专用砂浆。",
        "地上外墙采用B07级A5.0蒸压加气混凝土砌块，240厚，M5专用砂浆。",
        "地下内墙采用MU20混凝土实心砖，200厚，M10水泥砂浆。",
        "地上卫生间内墙采用MU10烧结多孔砖，120厚，M5水泥砂浆。",
        "地上内墙采用B05级A3.5蒸压加气混凝土砌块，100厚，Ma5.0砂浆。",
        "内隔墙采用蒸压加气混凝土砌块，具体等级详见设计。",
        "墙脚采用MU20水泥实心砖，200厚。",
    ]
    for i, row in enumerate(rows):
        text(f"{i+1}、" + row, 25, 219 - i * 12, 2.7)
    text("02  墙体材料表", 25, 116, 4)
    xs = [25, 105, 230, 282, 330, 395]
    ys = [105, 93, 81, 69]
    for y in ys:
        line((xs[0], y), (xs[-1], y))
    for x in xs:
        line((x, ys[-1]), (x, ys[0]))
    cells = [
        ["适用部位", "块材", "密度等级", "强度等级", "厚度"],
        ["地上内墙", "蒸压加气混凝土砌块", "B06", "A3.5", "200厚"],
        ["地下外墙", "混凝土实心砖", "—", "MU20", "240厚"],
    ]
    for ri, row in enumerate(cells):
        for ci, val in enumerate(row):
            text(val, xs[ci] + 3, ys[ri] - 8, 2.5)
    text(
        "说明：本图为程序生成的合成示例，仅用于功能演示，不作为工程设计或验收依据。",
        25,
        48,
        2.6,
    )
    line((25, 39), (395, 39))
    text("CAD / ATLAS", 25, 23, 4)
    text("建筑总说明  |  DEMO-A01  |  版本 01", 265, 23, 2.6)
    doc.saveas(path)
