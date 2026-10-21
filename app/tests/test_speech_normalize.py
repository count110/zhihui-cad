"""口语规范化：中文数字、符号、前缀、误识别纠正、不该改的地方不改。"""
import pytest

from zhihui.agent.speech_normalize import cn_to_number, normalize


@pytest.mark.parametrize("tok,val", [
    ("一百二十点五", 120.5), ("负十", -10), ("一百五", 150), ("二十", 20), ("两百", 200), ("一千二", 1200), ("十五", 15), ("二零二四", 2024),
    ("三十", 30), ("零点一", 0.1), ("十", 10), ("三万五千", 35000), ("一百零五", 105), ("二十五点五", 25.5), ("九", 9), ("负零点五", -0.5),
])
def test_cn_to_number(tok, val):
    assert cn_to_number(tok) == pytest.approx(val)


@pytest.mark.parametrize("src,want", [
    ("画一个半径为五十点五的圆", "画1个半径为50.5的圆"),
    ("把它往左移十", "把它往左移10"),
    ("半径二十", "半径20"),
    ("长一百二十宽八十", "长120宽80"),
    ("R二十", "R20"), ("H七", "H7"), ("Ra三点二", "Ra3.2"), ("M八螺栓", "M8螺栓"),
    ("直径三十的圆", "直径30的圆"),
    ("fai 三十", "⌀30"), ("phi三十", "⌀30"), ("斐五十", "⌀50"),
    ("正负零点一的公差", "±0.1的公差"), ("加减二的偏差", "±2的偏差"),
    ("圆心在一百逗号五十", "圆心在100,50"), ("坐标 100 逗号 50", "坐标 100,50"),
    ("相对三十逗号零", "@30,0"), ("艾特 30 逗号 0", "@30,0"), ("相对 一百 小于 四十五", "@100<45"),
    ("长一百角度四十五度", "长100<45度"), ("一百二十 小于 四十五", "120<45"),
    ("左下角在零零", "左下角在0,0"),
    ("第二个圆", "第2个圆"), ("最后一个", "最后1个"),
    ("全角１２３，ＡＢＣ", "全角123，ABC"),
])
def test_normalize_numbers_symbols_and_prefixes(src, want):
    assert normalize(src).text == want


@pytest.mark.parametrize("src,want", [
    ("画一个源，圆心在零零", "画1个圆，圆心在0,0"),
    ("把举行倒园角R五", "把矩形倒圆角R5"), ("到角五", "倒角5"), ("图成里建一个新的", "图层里建1个新的"), ("镜象这个圆", "镜像这个圆"), ("延申到边界", "延伸到边界"),
    ("修建多余的线", "修剪多余的线"), ("阵烈六个", "阵列6个"), ("标住直径", "标注直径"), ("画一条中心县", "画1条中心线"), ("加剖面县", "加剖面线"), ("视口比列一比二", "视口比例1比2"),
    ("线形比例调大", "线型比例调大"), ("画一个半径十的源", "画1个半径10的圆"), ("直径四十的源", "直径40的圆"), ("打开正焦", "打开正交"), ("撒销", "撤销"), ("加粗燥度", "加粗糙度"), ("加平型度公差", "加平行度公差"), ("加同新约束", "加同心约束"),
])
def test_normalize_asr_errors(src, want):
    assert normalize(src).text == want


@pytest.mark.parametrize("src", [
    "切换到三维建模空间", "二维草图", "看三视图", "六角螺栓", "统一使用中心线", "一般情况下用粗实线", "同一个图层", "唯一的线", "零件图", "十字线", "十分重要", "万一失败了", "之一", "100,50", "C8.center", "L2", "@30,0",
])
def test_normalize_leaves_non_numeric_words_alone(src):
    assert normalize(src).text == src


def test_changes_are_reported_and_original_is_kept():
    r = normalize("画个源，半径二十")
    assert r.original == "画个源，半径二十" and r.text == "画个圆，半径20" and r.changed
    assert {c["rule"] for c in r.changes} >= {"中文数字"}
    assert normalize("L 0,0 @100,0").changes == [] and not normalize("L 0,0 @100,0").changed
