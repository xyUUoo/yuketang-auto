# -*- coding: utf-8 -*-
"""打包前自动递增 APP_VERSION 第三位（1.0.0 → 1.0.1），写回 gui.py"""
import re
import sys

PATH = "gui.py"
s = open(PATH, encoding="utf-8").read()
m = re.search(r'APP_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', s)
if not m:
    print("未找到 APP_VERSION，中止打包")
    sys.exit(1)
ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
new = f"{ver[0]}.{ver[1]}.{ver[2] + 1}"
s = s.replace(m.group(0), f'APP_VERSION = "{new}"')
open(PATH, "w", encoding="utf-8").write(s)
print(f"版本号已递增：{'.'.join(map(str, ver))} -> {new}")
