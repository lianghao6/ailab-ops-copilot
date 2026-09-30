"""六节课的内容定义。每节课由 deck.py 渲染成一个 PDF。"""

from . import l1, l2, l3, l4, l5, l6

ALL = [l1.LESSON, l2.LESSON, l3.LESSON, l4.LESSON, l5.LESSON, l6.LESSON]

__all__ = ["ALL", "l1", "l2", "l3", "l4", "l5", "l6"]
