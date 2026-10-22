// Interruptible springs (Apple's damping/response model). Every animation starts from the
// current value and can be stopped and re-targeted at any frame, carrying its velocity.

const reduced = matchMedia('(prefers-reduced-motion: reduce)');
export const prefersReducedMotion = () => reduced.matches;

/**
 * Animate several numeric values towards `to`.
 * damping 1 = no overshoot (default); response = seconds to (nearly) reach the target.
 * precision: per-key settle threshold (default 0.5, i.e. half a pixel).
 */
export function animate(from, to, { damping = 1, response = 0.35, velocity = {}, precision = {}, onUpdate } = {}) {
  const keys = Object.keys(to);
  const x = { ...from };
  const v = Object.fromEntries(keys.map((k) => [k, velocity[k] ?? 0]));
  const omega = (2 * Math.PI) / response;
  const stiffness = omega * omega;
  const friction = 2 * damping * omega;
  let resolveDone;
  const done = new Promise((r) => { resolveDone = r; });
  let raf = 0;
  let stopped = false;
  let last = performance.now();

  const finish = (ok) => { resolveDone(ok); };

  if (prefersReducedMotion()) {
    Object.assign(x, to);
    onUpdate?.(x);
    finish(true);
    return { stop() {}, value: () => ({ ...x }), velocity: () => ({ ...v }), done };
  }

  const tick = (now) => {
    if (stopped) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const steps = Math.max(1, Math.ceil(dt / 0.004));
    const h = dt / steps;
    let settled = true;
    for (const k of keys) {
      for (let i = 0; i < steps; i++) {
        const a = -stiffness * (x[k] - to[k]) - friction * v[k];
        v[k] += a * h;
        x[k] += v[k] * h;
      }
      const eps = precision[k] ?? 0.5;
      if (Math.abs(x[k] - to[k]) > eps || Math.abs(v[k]) > eps * 20) settled = false;
    }
    if (settled) {
      Object.assign(x, to);
      onUpdate?.(x);
      finish(true);
      return;
    }
    onUpdate?.(x);
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);

  return {
    stop() { if (!stopped) { stopped = true; cancelAnimationFrame(raf); finish(false); } },
    value: () => ({ ...x }),
    velocity: () => ({ ...v }),
    done,
  };
}

/** Where a flick would come to rest (Apple's exponential deceleration). velocity in px/s. */
export function project(velocity, rate = 0.998) {
  return ((velocity / 1000) * rate) / (1 - rate);
}

/** Progressive resistance past a boundary. */
export function rubberband(overshoot, dimension, c = 0.55) {
  return (overshoot * dimension * c) / (dimension + c * Math.abs(overshoot));
}

/** Pointer velocity (px/s) from a short history of {x, y, t}. */
export function velocityOf(history) {
  const now = history[history.length - 1];
  const past = history.find((p) => now.t - p.t < 100) ?? history[0];
  const dt = (now.t - past.t) / 1000;
  if (dt <= 0) return { x: 0, y: 0 };
  return { x: (now.x - past.x) / dt, y: (now.y - past.y) / dt };
}
