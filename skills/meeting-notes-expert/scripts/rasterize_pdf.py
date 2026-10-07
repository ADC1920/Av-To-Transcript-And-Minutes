# -*- coding: utf-8 -*-
"""把渲染出的 PDF 逐页栅格化为 PNG（供交付前视觉检查）。

用法：
    python rasterize_pdf.py <pdf> <out_dir> [--dpi 130]

会在 out_dir 下输出 page-01.png、page-02.png …（输出前清理同目录旧 PNG）。
"""

import os
import sys

import pymupdf


def main(pdf_path, out_dir, dpi=130):
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(out_dir):
        if name.endswith(".png"):
            os.remove(os.path.join(out_dir, name))
    doc = pymupdf.open(pdf_path)
    for i, page in enumerate(doc):
        page.get_pixmap(dpi=dpi).save(os.path.join(out_dir, f"page-{i + 1:02d}.png"))
    print(f"pages={doc.page_count} -> {out_dir}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if len(argv) < 2:
        sys.stderr.write(__doc__ + "\n")
        raise SystemExit(2)
    dpi = 130
    if "--dpi" in argv:
        idx = argv.index("--dpi")
        dpi = int(argv[idx + 1])
        argv = argv[:idx] + argv[idx + 2:]
    main(argv[0], argv[1], dpi=dpi)
