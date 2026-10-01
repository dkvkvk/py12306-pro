#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""py12306-pro 统一入口。

命令分发见 railkit/cli.py；抢票业务逻辑仍在 py12306/ 与 upstream_entry.py（原 main.py）。
"""

from __future__ import annotations

import sys

from railkit.cli import main

if __name__ == "__main__":
    sys.exit(main())
