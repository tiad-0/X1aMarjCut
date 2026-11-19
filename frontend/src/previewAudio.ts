// The live preview's bleeps (1.2.45): what backend/sharkyclips/edit/audiomix.py apply_bleeps()
// does to the clip's sound, played while the SOURCE video runs in the preview.
//
// One source of truth: the server's edit plan. plan.bleep_src are the source-time ranges the
// render bleeps (audiomix gets plan.bleeps = plan.out_ranges(bleep_src): the same moments in
// output time). The envelope below is a copy of apply_bleeps (sample positions rounded the way
// Python's round() does, 12 ms raised-cosine fades, the tone from zero phase); the Web Audio
// part schedules it on the audio clock ahead of time, so it is not tied to the frame rate.
// test_preview_bleeps.py runs this file with Node and compares it with the Python side.

export const SR = 48000;
export const BLEEP_HZ = 1000;
export const BLEEP_FADE = 0.012;

export type Range = [number, number];

/** Python's round(): halves go to the even neighbour. */
export function pyRound(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (Math.abs(d - 0.5) < 1e-9) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}

/** audiomix._n */
export function n(t: number, sr = SR): number {
  return pyRound(t * sr);
}

/** The fade curve of apply_bleeps: np.linspace(0, pi, r) through 0.5 - 0.5 cos. */
export function fadeCurve(r: number): Float32Array {
  const out = new Float32Array(r);
  for (let k = 0; k < r; k++) out[k] = 0.5 - 0.5 * Math.cos(r > 1 ? (Math.PI * k) / (r - 1) : 0);
  return out;
}

/** The bleep envelope (0 = untouched sound, 1 = fully bleeped) at sample s, exactly as
 * apply_bleeps builds it: ranges at sample positions [n(a), n(b)), fades of min(fade, len / 2). */
export function envAtSample(s: number, ranges: Range[], fade = BLEEP_FADE, sr = SR): number {
  const fadeN = n(fade, sr);
  for (const [a, b] of ranges) {
    const ia = Math.max(0, n(a, sr));
    const ib = n(b, sr);
    if (ib - ia < 2 || s < ia || s >= ib) continue;
    const len = ib - ia;
    const r = Math.min(fadeN, Math.floor(len / 2));
    const k = s - ia;
    if (!r) return 1;
    const c = fadeCurve(r);
    if (k >= len - r) return c[len - 1 - k];
    if (k < r) return c[k];
    return 1;
  }
  return 0;
}

/** Source ranges -> output ranges through the kept parts (plan.out_ranges). */
export function outRanges(ranges: Range[], keep: Range[]): Range[] {
  const out: Range[] = [];
  let off = 0;
  const offs = keep.map(([a, b]) => {
    const o = off;
    off += b - a;
    return o;
  });
  for (const [a, b] of ranges) {
    keep.forEach(([ka, kb], i) => {
      const lo = Math.max(a, ka);
      const hi = Math.min(b, kb);
      if (hi > lo) out.push([offs[i] + lo - ka, offs[i] + hi - ka]);
    });
  }
  out.sort((x, y) => x[0] - y[0]);
  const merged: Range[] = [];
  for (const r of out) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1] + 0.001) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  return merged;
}

/** One envelope step for the scheduler: at source time `t` (seconds) the envelope becomes the
 * curve (sampled every 1/sr), from the range's own sample grid. */
export interface Step {
  t: number;
  curve: Float32Array;
  /** seconds the curve lasts (curve.length / sr) */
  dur: number;
}

/** The envelope of each range as (fade in, hold, fade out) steps, on the render's sample grid. */
export function steps(ranges: Range[], fade = BLEEP_FADE, sr = SR): { a: number; b: number; up: Step; down: Step }[] {
  const fadeN = n(fade, sr);
  const out: { a: number; b: number; up: Step; down: Step }[] = [];
  for (const [a, b] of ranges) {
    const ia = Math.max(0, n(a, sr));
    const ib = n(b, sr);
    if (ib - ia < 2) continue;
    const r = Math.max(1, Math.min(fadeN, Math.floor((ib - ia) / 2)));
    const c = fadeCurve(r);
    const rev = Float32Array.from(c).reverse();
    out.push({ a: ia / sr, b: ib / sr, up: { t: ia / sr, curve: c, dur: r / sr }, down: { t: (ib - r) / sr, curve: rev, dur: r / sr } });
  }
  return out;
}

// ------------------------------------------------------------------ Web Audio (browser only)

interface Graph {
  ctx: AudioContext;
  voice: GainNode;
  tone: GainNode;
  osc: OscillatorNode;
}

const graphs = new WeakMap<HTMLMediaElement, Graph>();

/** The video's sound through a gain (ducked in a bleep) + a 1 kHz tone (raised in a bleep).
 * createMediaElementSource can be made once per element: kept for the element's lifetime. */
function graphFor(v: HTMLMediaElement): Graph | null {
  const got = graphs.get(v);
  if (got) return got;
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx({ latencyHint: "interactive" });
    const src = ctx.createMediaElementSource(v);
    const voice = ctx.createGain();
    const tone = ctx.createGain();
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.value = BLEEP_HZ;
    tone.gain.value = 0;
    src.connect(voice).connect(ctx.destination);
    osc.connect(tone).connect(ctx.destination);
    osc.start();
    const g = { ctx, voice, tone, osc };
    graphs.set(v, g);
    return g;
  } catch {
    return null;
  }
}

/** The live gains of a video's bleep graph (browser checks): [voice, tone] or null. */
export function gainsOf(v: HTMLMediaElement): [number, number] | null {
  const g = graphs.get(v);
  return g ? [g.voice.gain.value, g.tone.gain.value] : null;
}

export interface BleepPlan {
  ranges: Range[];
  mode: "bleep" | "mute" | "off";
  sound: string;
  fade?: number;
}

/** Bleeps the preview video live. Returns a stop function. Times come from the video
 * (currentTime / playbackRate), scheduled on the audio clock: rescheduled on play, seek, rate
 * changes and every couple of seconds (clock drift), never mid-fade. */
export function attachBleeps(v: HTMLVideoElement, plan: BleepPlan): () => void {
  const on = plan.mode !== "off" && plan.ranges.length > 0;
  let g: Graph | null = graphs.get(v) ?? null;
  if (!on && !g) return () => {};
  const st = steps(plan.ranges, plan.fade ?? BLEEP_FADE);
  const level = () => (plan.mode === "bleep" ? 0.16 : 0); // the sine at about the voice level (unit RMS x 0.16)
  let timer = 0;

  const clear = () => {
    if (!g) return;
    const now = g.ctx.currentTime;
    for (const p of [g.voice.gain, g.tone.gain]) {
      p.cancelScheduledValues(0);
      p.setValueAtTime(p.value, now);
    }
  };
  const reset = () => {
    if (!g) return;
    clear();
    const now = g.ctx.currentTime;
    g.voice.gain.setValueAtTime(1, now);
    g.tone.gain.setValueAtTime(0, now);
  };

  const schedule = () => {
    if (!on) {
      reset();
      return;
    }
    g = g ?? graphFor(v);
    if (!g) return;
    if (g.ctx.state === "suspended") g.ctx.resume().catch(() => {});
    g.osc.frequency.setValueAtTime(plan.sound === "boop" ? 460 : BLEEP_HZ, g.ctx.currentTime); // boop: a lower, softer tone
    clear();
    const rate = v.playbackRate || 1;
    const now = g.ctx.currentTime;
    const t0 = v.currentTime;
    const at = (t: number) => now + (t - t0) / rate;
    const L = level();
    // where we are now (inside a bleep: its envelope value right now)
    const e = envAtSample(n(t0), plan.ranges, plan.fade ?? BLEEP_FADE);
    g.voice.gain.setValueAtTime(1 - e, now);
    g.tone.gain.setValueAtTime(v.paused ? 0 : e * L, now); // paused: the video is silent, so is the tone
    if (v.paused) return;
    for (const s of st) {
      if (s.b <= t0) continue;
      for (const step of [s.up, s.down]) {
        if (step.t < t0) continue; // passed / inside this fade: the value set above holds until the next one
        const T = at(step.t);
        const dur = step.dur / rate;
        const voiceCurve = Float32Array.from(step.curve, (x) => 1 - x);
        const toneCurve = Float32Array.from(step.curve, (x) => x * L);
        try {
          g.voice.gain.setValueCurveAtTime(voiceCurve, T, dur);
          g.tone.gain.setValueCurveAtTime(toneCurve, T, dur);
        } catch {
          /* overlapping curves (a seek right on a fade): the next reschedule fixes it */
        }
      }
    }
  };

  // the oscillator restarts at zero phase for each bleep in the render; a free-running sine is
  // fine for listening (the timing is what has to match)
  const events = ["play", "playing", "seeked", "ratechange", "pause"] as const;
  events.forEach((ev) => v.addEventListener(ev, schedule));
  const tick = () => {
    // drift fix: reschedule when no fade is near (never in the middle of one)
    const t = v.currentTime;
    const near = st.some((s) => (t > s.a - 0.25 && t < s.a + 0.05) || (t > s.b - 0.25 && t < s.b + 0.05));
    if (!v.paused && !near) schedule();
  };
  timer = window.setInterval(tick, 2000);
  schedule();
  return () => {
    window.clearInterval(timer);
    events.forEach((ev) => v.removeEventListener(ev, schedule));
    reset();
  };
}
