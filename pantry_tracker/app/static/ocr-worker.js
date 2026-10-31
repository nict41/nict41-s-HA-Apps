/* PP-OCRv4 browser worker. Runtime and model files are installed by Docker. */
const ORT_VERSION = "1.31.1";
const DET_PATH = "./ocr/ch_PP-OCRv4_det_infer.onnx?v=" + ORT_VERSION;
const REC_PATH = "./ocr/ch_PP-OCRv4_rec_infer.onnx?v=" + ORT_VERSION;
const KEYS_PATH = "./ocr/ppocr_keys_v1.txt?v=" + ORT_VERSION;
const SOFTENED_WIDTH = 740;
const SOFTENED_BLUR_DIVISOR = 494;
let detector, recognizer, keys, loading, bitmap;
const mean = [0.485, 0.456, 0.406], std = [0.229, 0.224, 0.225];

async function loadModels() {
  if (detector) return;
  if (loading) return loading;
  loading = (async () => {
    importScripts("./ocr/ort.wasm.min.js?v=" + ORT_VERSION);
    ort.env.wasm.numThreads = 1;
    const wasm = new URL("ocr/", self.location).href;
    ort.env.wasm.wasmPaths = {
      mjs: `${wasm}ort-wasm-simd-threaded.mjs?v=${ORT_VERSION}`,
      wasm: `${wasm}ort-wasm-simd-threaded.wasm?v=${ORT_VERSION}`,
    };
    const [detBytes, recBytes, keyResponse] = await Promise.all([
      fetch(DET_PATH).then((r) => { if (!r.ok) throw Error("Could not load detector"); return r.arrayBuffer(); }),
      fetch(REC_PATH).then((r) => { if (!r.ok) throw Error("Could not load recognizer"); return r.arrayBuffer(); }),
      fetch(KEYS_PATH).then((r) => { if (!r.ok) throw Error("Could not load OCR keys"); return r.text(); }),
    ]);
    keys = ["blank", ...keyResponse.replace(/\r/g, "").split("\n").filter((s) => s !== ""), " "];
    [detector, recognizer] = await Promise.all([
      ort.InferenceSession.create(detBytes, { executionProviders: ["wasm"] }),
      ort.InferenceSession.create(recBytes, { executionProviders: ["wasm"] }),
    ]);
  })();
  try { await loading; } catch (error) { loading = null; throw error; }
}

function canvasPixels(canvas, width, height) {
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.clearRect(0, 0, width, height);
  ctx.drawImage(bitmap, 0, 0, width, height);
  return ctx.getImageData(0, 0, width, height).data;
}

async function detect(canvas, width, height) {
  const scale = Math.min(960 / Math.max(width, height), width < 600 ? 3 : 1);
  const w = Math.max(32, Math.round(width * scale / 32) * 32);
  const h = Math.max(32, Math.round(height * scale / 32) * 32);
  canvas.width = w; canvas.height = h;
  const pixels = canvasPixels(canvas, w, h), plane = w * h, input = new Float32Array(3 * plane);
  for (let i = 0; i < plane; i++) for (let c = 0; c < 3; c++) input[c * plane + i] = (pixels[i * 4 + c] / 255 - mean[c]) / std[c];
  const result = await detector.run({ [detector.inputNames[0]]: new ort.Tensor("float32", input, [1, 3, h, w]) });
  const probabilities = result[detector.outputNames[0]].data, seen = new Uint8Array(plane), boxes = [];
  for (let i = 0; i < plane; i++) {
    if (seen[i] || probabilities[i] < 0.3) continue;
    const stack = [i]; seen[i] = 1;
    let x0 = w, x1 = -1, y0 = h, y1 = -1, n = 0, sum = 0;
    while (stack.length) {
      const p = stack.pop(), x = p % w, y = (p - x) / w;
      n++; sum += probabilities[p]; x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
      for (const q of [x > 0 ? p - 1 : -1, x < w - 1 ? p + 1 : -1, p - w, p + w])
        if (q >= 0 && q < plane && !seen[q] && probabilities[q] >= 0.3) { seen[q] = 1; stack.push(q); }
    }
    if (n < 20 || sum / n < 0.5) continue;
    const bw = x1 - x0 + 1, bh = y1 - y0 + 1, e = Math.round(1.5 * bw * bh / (2 * (bw + bh)));
    boxes.push([Math.max(0, x0 - e) * width / w, Math.max(0, y0 - e) * height / h,
      Math.min(w, x1 + e + 1) * width / w, Math.min(h, y1 + e + 1) * height / h].map(Math.round));
  }
  return boxes.sort((a, b) => a[1] - b[1] || a[0] - b[0]);
}

async function recognize(canvas, box) {
  const [left, top, right, bottom] = box, width = right - left, height = bottom - top;
  if (width < 4 || height < 4) return "";
  const outWidth = Math.min(1280, Math.max(48, Math.round(width * 48 / height)));
  canvas.width = outWidth; canvas.height = 48;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.clearRect(0, 0, outWidth, 48);
  ctx.drawImage(bitmap, left, top, width, height, 0, 0, outWidth, 48);
  const pixels = ctx.getImageData(0, 0, outWidth, 48).data, plane = outWidth * 48, input = new Float32Array(3 * plane);
  for (let i = 0; i < plane; i++) for (let c = 0; c < 3; c++) input[c * plane + i] = (pixels[i * 4 + c] / 255 - 0.5) / 0.5;
  const result = await recognizer.run({ [recognizer.inputNames[0]]: new ort.Tensor("float32", input, [1, 3, 48, outWidth]) });
  const tensor = result[recognizer.outputNames[0]], [, steps, count] = tensor.dims;
  let text = "", last = -1;
  for (let t = 0; t < steps; t++) {
    let index = 0, value = -Infinity;
    for (let c = 0; c < count; c++) if (tensor.data[t * count + c] > value) { value = tensor.data[t * count + c]; index = c; }
    if (index !== last && index !== 0) text += keys[index] || "";
    last = index;
  }
  return text;
}

// Separates tightly spaced dot-printed lines, which the detector otherwise
// boxes together and the recognizer can't read: the dots merge into strokes
// and the gap between the lines survives.
async function soften(source) {
  const scale = Math.min(1, SOFTENED_WIDTH / source.width);
  const canvas = new OffscreenCanvas(Math.round(source.width * scale), Math.round(source.height * scale));
  const blurred = new OffscreenCanvas(source.width, source.height);
  const blurContext = blurred.getContext("2d");
  blurContext.filter = `blur(${source.width / SOFTENED_BLUR_DIVISOR}px)`;
  blurContext.drawImage(source, 0, 0);
  const context = canvas.getContext("2d");
  context.imageSmoothingQuality = "high";
  context.drawImage(blurred, 0, 0, canvas.width, canvas.height);
  return createImageBitmap(canvas);
}

async function read(file, soft, sourceBitmap = false) {
  await loadModels();
  bitmap = sourceBitmap ? file : await createImageBitmap(file);
  if (soft) {
    const original = bitmap;
    bitmap = await soften(original);
    original.close();
  }
  const canvas = new OffscreenCanvas(1, 1);
  canvas.getContext("2d").imageSmoothingQuality = "high";
  try {
    const lines = [];
    // One at a time: an inference session can't run two reads at once.
    for (const box of await detect(canvas, bitmap.width, bitmap.height)) lines.push(await recognize(canvas, box));
    return lines;
  } finally {
    bitmap.close();
    bitmap = null;
  }
}

let processing = Promise.resolve();
self.onmessage = async ({ data }) => {
  if (data?.type === "init") { try { await loadModels(); self.postMessage({ type: "ready" }); } catch (error) { self.postMessage({ type: "init-error", error: String(error) }); } return; }
  if (!data || data.id === undefined) return;
  processing = processing.then(async () => {
    try { self.postMessage({ id: data.id, lines: await read(data.bitmap || data.file, data.soft, Boolean(data.bitmap)) }); }
    catch (error) {
      if (data.bitmap) data.bitmap.close();
      self.postMessage({ id: data.id, error: String(error?.message || error) });
    }
  });
};
