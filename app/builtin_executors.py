"""内建执行器注册入口。

导入一个执行器模块，它就会在导入时把自己注册进 :mod:`app.executors`。
新增一种计算/文件类型时，只需要在下面**加一行 import**——
``app/workflow.py`` 与 ``app/worker.py`` 都不需要改动。
"""

from __future__ import annotations

from . import excel_executor

__all__ = ["excel_executor"]
