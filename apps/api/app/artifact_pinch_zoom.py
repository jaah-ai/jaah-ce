"""The pinch-zoom script appended to an HTML artifact served for in-place viewing (`routes/attachments.serve_artifact`).

A pinch inside an iframe never zooms the frame: the browser zooms the TOP page, so on a phone the console's header,
tabs and layout scale off screen while the artifact stays as it was. The console cannot see touches inside the
sandboxed frame (an opaque origin), so the frame's own document handles them: it refuses the browser's page pinch
(`touch-action` plus a non-passive `touchmove`/`gesture*` `preventDefault`) and magnifies its body instead, keeping the
point between the fingers still, with native scroll to pan. Back at 1× every inline style it touched is restored.

Appended, never prepended: a script before `<!doctype html>` would drop the document into quirks mode. It runs under the
route's CSP (`script-src 'unsafe-inline'`) like the artifact's own scripts, and on the artifact's opaque origin it reaches
nothing of the console's. Its listeners sit on `window`, registered after the artifact's own, so a two-finger gesture the
artifact already handled (`defaultPrevented`) is left to it; the root's `touch-action` is overridden all the same, which
also ends double-tap zoom in the frame.

Not reached: a report over `MAX_PROXY_BYTES` (served by redirect, its bytes never pass here), a UTF-16 document (ASCII
bytes appended to it would be garbage, so it is skipped), and one whose markup ends inside an unclosed `<script>`,
`<textarea>` or comment (the script becomes text there). Each keeps the old page-zoom behaviour; none is less safe.
"""

from __future__ import annotations

#: A UTF-16 byte-order mark: such a document cannot take ASCII bytes appended.
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

#: The most the frame magnifies; past it a pinch-in stops growing.
MAX_SCALE = 5

PINCH_ZOOM_SCRIPT = (
    """<script>(function () {
  var MAX = %d, doc = document, root = doc.documentElement, scale = 1, pinch = null, saved = null;
  root.style.setProperty('touch-action', 'pan-x pan-y', 'important');
  function gap(t) { return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY); }
  function mid(t) { return { x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2 }; }
  function zoom(next) {
    var body = doc.body;
    if (!saved) {
      saved = { scale: body.style.scale, origin: body.style.transformOrigin,
        rootOverflow: root.style.getPropertyValue('overflow'), bodyOverflow: body.style.getPropertyValue('overflow') };
    }
    scale = Math.min(MAX, Math.max(1, next));
    if (scale <= 1.01) {
      scale = 1;
      body.style.scale = saved.scale;
      body.style.transformOrigin = saved.origin;
      root.style.setProperty('overflow', saved.rootOverflow);
      body.style.setProperty('overflow', saved.bodyOverflow);
      saved = null;
      return;
    }
    body.style.transformOrigin = '0 0';
    body.style.scale = String(scale);
    root.style.setProperty('overflow', 'visible', 'important');
    body.style.setProperty('overflow', 'visible', 'important');
  }
  function begin(t) {
    var m = mid(t), box = doc.body.getBoundingClientRect();
    // The body's layout corner (its margin included): `scale` grows from it, so the anchor is measured from it.
    var left = box.left + window.scrollX, top = box.top + window.scrollY;
    pinch = { gap: gap(t), scale: scale, left: left, top: top,
      x: (window.scrollX + m.x - left) / scale, y: (window.scrollY + m.y - top) / scale };
  }
  window.addEventListener('touchstart', function (e) {
    if (e.touches.length < 2 || e.defaultPrevented || !doc.body) return;
    begin(e.touches);
    e.preventDefault();
  }, { passive: false });
  window.addEventListener('touchmove', function (e) {
    if (!pinch || e.touches.length < 2 || e.defaultPrevented) return;
    e.preventDefault();
    var m = mid(e.touches);
    zoom(pinch.scale * gap(e.touches) / Math.max(pinch.gap, 1));
    window.scrollTo(pinch.left + pinch.x * scale - m.x, pinch.top + pinch.y * scale - m.y);
  }, { passive: false });
  // A lifted finger re-anchors on the pair still down, so the zoom never jumps to another finger's gap.
  window.addEventListener('touchend', function (e) { if (e.touches.length < 2) pinch = null; else begin(e.touches); });
  window.addEventListener('touchcancel', function () { pinch = null; });
  ['gesturestart', 'gesturechange'].forEach(function (type) {
    window.addEventListener(type, function (e) { e.preventDefault(); }, { passive: false });
  });
})();</script>"""
    % MAX_SCALE
).encode()
