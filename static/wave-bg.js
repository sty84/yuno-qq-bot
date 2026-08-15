// 交互波浪背景：参考 DeepSeek 首页的“鼠标波动”效果。
// 用法：页面里放 <canvas id="bg-canvas"></canvas>，并引入本脚本。
(function () {
  const canvas = document.getElementById('bg-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, 2);
  const ripples = [];
  const mouse = { x: -9999, y: -9999 };
  const COLORS = ['rgba(59,130,246,', 'rgba(139,92,246,', 'rgba(34,211,238,'];

  function resize() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = W * DPR;
    canvas.height = H * DPR;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  function addRipple(x, y) {
    ripples.push({
      x, y,
      r: 4 + Math.random() * 10,
      maxR: 120 + Math.random() * 160,
      alpha: 0.5 + Math.random() * 0.3,
      speed: 1.2 + Math.random() * 1.5,
      color: COLORS[Math.floor(Math.random() * COLORS.length)],
    });
    if (ripples.length > 40) ripples.shift();
  }

  function drawBackground(t) {
    const grad = ctx.createLinearGradient(0, 0, W, H);
    grad.addColorStop(0, '#0b1120');
    grad.addColorStop(0.5, '#0f172a');
    grad.addColorStop(1, '#111827');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);

    // 环境波浪：几层缓慢流动的正弦线
    ctx.lineWidth = 1.2;
    for (let layer = 0; layer < 3; layer++) {
      const baseY = H * (0.3 + layer * 0.2);
      const amp = 14 + layer * 8;
      const freq = 0.008 + layer * 0.003;
      const speed = 0.0006 + layer * 0.0002;
      ctx.strokeStyle = COLORS[layer] + (0.12 + layer * 0.04) + ')';
      ctx.beginPath();
      for (let x = 0; x <= W; x += 6) {
        const y = baseY + Math.sin(x * freq + t * speed) * amp + Math.sin(x * 0.02 + t * 0.001) * 6;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }

  function drawRipples() {
    for (let i = ripples.length - 1; i >= 0; i--) {
      const r = ripples[i];
      r.r += r.speed;
      r.alpha *= 0.985;
      if (r.r > r.maxR || r.alpha < 0.02) {
        ripples.splice(i, 1);
        continue;
      }
      ctx.beginPath();
      ctx.arc(r.x, r.y, r.r, 0, Math.PI * 2);
      ctx.strokeStyle = r.color + r.alpha + ')';
      ctx.lineWidth = 2;
      ctx.stroke();

      // 内圈淡光
      ctx.beginPath();
      ctx.arc(r.x, r.y, Math.max(2, r.r * 0.55), 0, Math.PI * 2);
      ctx.strokeStyle = r.color + (r.alpha * 0.35) + ')';
      ctx.lineWidth = 5;
      ctx.stroke();
    }
  }

  function frame(t) {
    drawBackground(t);
    // 鼠标持续产生微弱波纹
    if (Math.random() < 0.18) addRipple(mouse.x + (Math.random() - 0.5) * 30, mouse.y + (Math.random() - 0.5) * 30);
    drawRipples();
    requestAnimationFrame(frame);
  }

  window.addEventListener('resize', resize);
  window.addEventListener('mousemove', (e) => {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
    addRipple(e.clientX, e.clientY);
  });
  window.addEventListener('touchmove', (e) => {
    const t = e.touches[0];
    if (t) {
      mouse.x = t.clientX;
      mouse.y = t.clientY;
      addRipple(t.clientX, t.clientY);
    }
  }, { passive: true });

  resize();
  requestAnimationFrame(frame);
})();
