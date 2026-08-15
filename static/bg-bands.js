// 年轻配色背景：几条宽色带动态飘动 + 高斯模糊，无鼠标波动。
// 用法：页面里放 <canvas id="bg-canvas"></canvas>，并引入本脚本。
(function () {
  const canvas = document.getElementById('bg-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, 2);

  const BANDS = [
    { color: 'rgba(244,114,182,', x: 0.2, y: 0.3, rx: 0.45, ry: 0.35, sx: 0.00020, sy: 0.00015, phase: 0 },
    { color: 'rgba(167,139,250,', x: 0.7, y: 0.2, rx: 0.5, ry: 0.3, sx: -0.00018, sy: 0.00012, phase: 1.2 },
    { color: 'rgba(34,211,238,', x: 0.3, y: 0.75, rx: 0.4, ry: 0.35, sx: 0.00015, sy: -0.00018, phase: 2.1 },
    { color: 'rgba(52,211,153,', x: 0.8, y: 0.7, rx: 0.45, ry: 0.3, sx: -0.00012, sy: 0.00016, phase: 3.0 },
    { color: 'rgba(251,191,36,', x: 0.5, y: 0.5, rx: 0.55, ry: 0.4, sx: 0.00010, sy: 0.00010, phase: 4.2 },
  ];

  function resize() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = W * DPR;
    canvas.height = H * DPR;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  function draw(t) {
    // 年轻明亮的底色
    const grad = ctx.createLinearGradient(0, 0, W, H);
    grad.addColorStop(0, '#fdf2f8');
    grad.addColorStop(0.5, '#f0f4ff');
    grad.addColorStop(1, '#ecfeff');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);

    ctx.save();
    ctx.filter = 'blur(70px)'; // 高斯模糊，让色带柔和
    for (let i = 0; i < BANDS.length; i++) {
      const b = BANDS[i];
      const cx = W * (b.x + Math.sin(t * b.sx + b.phase) * 0.12);
      const cy = H * (b.y + Math.cos(t * b.sy + b.phase) * 0.12);
      const rx = W * b.rx;
      const ry = H * b.ry;
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(rx, ry));
      g.addColorStop(0, b.color + '0.55)');
      g.addColorStop(1, b.color + '0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  function frame(t) {
    draw(t);
    requestAnimationFrame(frame);
  }

  window.addEventListener('resize', resize);
  resize();
  requestAnimationFrame(frame);
})();
