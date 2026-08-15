// 交互波浪背景：参考 DeepSeek 首页的“鼠标波动”效果。
// 已调低波动频率，并增加模糊柔化，配色改为靛紫/青绿。
// 用法：页面里放 <canvas id="bg-canvas"></canvas>，并引入本脚本。
(function () {
  const canvas = document.getElementById('bg-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, 2);
  const ripples = [];
  const mouse = { x: -9999, y: -9999 };
  const COLORS = ['rgba(99,102,241,', 'rgba(168,85,247,', 'rgba(45,212,191,'];

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
      r: 6 + Math.random() * 8,
      maxR: 160 + Math.random() * 140,
      alpha: 0.35 + Math.random() * 0.25,
      speed: 0.7 + Math.random() * 0.8,
      color: COLORS[Math.floor(Math.random() * COLORS.length)],
    });
    if (ripples.length > 30) ripples.shift();
  }

  function drawBackground(t) {
    const grad = ctx.createLinearGradient(0, 0, W, H);
    grad.addColorStop(0, '#0b0f1a');
    grad.addColorStop(0.5, '#111827');
    grad.addColorStop(1, '#0f172a');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);

    // 环境波浪：频率调低、速度放慢，并加模糊柔化
    ctx.save();
    ctx.filter = 'blur(3px)';
    ctx.lineWidth = 1.6;
    for (let layer = 0; layer < 3; layer++) {
      const baseY = H * (0.3 + layer * 0.2);
      const amp = 18 + layer * 7;
      const freq = 0.004 + layer * 0.0015;   // 更低频
      const speed = 0.0003 + layer * 0.0001; // 更慢
      ctx.strokeStyle = COLORS[layer] + (0.18 + layer * 0.05) + ')';
      ctx.beginPath();
      for (let x = 0; x <= W; x += 8) {
        const y = baseY + Math.sin(x * freq + t * speed) * amp + Math.sin(x * 0.012 + t * 0.0006) * 8;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawRipples() {
    ctx.save();
    ctx.filter = 'blur(2.5px)';
    ctx.shadowColor = 'rgba(120,120,255,0.5)';
    ctx.shadowBlur = 14;
    for (let i = ripples.length - 1; i >= 0; i--) {
      const r = ripples[i];
      r.r += r.speed;
      r.alpha *= 0.988;
      if (r.r > r.maxR || r.alpha < 0.02) {
        ripples.splice(i, 1);
        continue;
      }
      ctx.beginPath();
      ctx.arc(r.x, r.y, r.r, 0, Math.PI * 2);
      ctx.strokeStyle = r.color + r.alpha + ')';
      ctx.lineWidth = 2.5;
      ctx.stroke();

      // 内圈淡光
      ctx.beginPath();
      ctx.arc(r.x, r.y, Math.max(2, r.r * 0.55), 0, Math.PI * 2);
      ctx.strokeStyle = r.color + (r.alpha * 0.3) + ')';
      ctx.lineWidth = 8;
      ctx.stroke();
    }
    ctx.restore();
  }

  function frame(t) {
    drawBackground(t);
    // 鼠标持续产生微弱波纹：频率降低，避免太密集
    if (Math.random() < 0.08) addRipple(mouse.x + (Math.random() - 0.5) * 40, mouse.y + (Math.random() - 0.5) * 40);
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
