"""The ONE process-wide matplotlib lock (WP2 R2l / R2m).

matplotlib is not thread-safe — figures, Agg canvases, the colormap registry, the PDF backend,
``qsave``'s pyplot / backend switching and the rcParams touched by the CJK font hook are all
process-global — and the workbench runs export jobs, lazy PNG previews and MCP flowchart renders
in concurrent threads. Every matplotlib entry of the process holds this lock:
``sheet.render_preview`` (lazy PNG, flowchart previews, export PNG) and ``deliverable.rasterize``
/ ``deliverable.write_pdf`` (writer and checker). It lives in its own import-free module so
``sheet`` and ``deliverable`` can share it without an import cycle. Reentrant; never held
together with the checker's PDF-renderer lock.
"""

from __future__ import annotations

import threading

MPL_LOCK = threading.RLock()
