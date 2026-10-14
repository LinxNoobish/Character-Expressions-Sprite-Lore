// Shows the prompt embedded by anime2img.py on the node as soon as an
// image is picked, uploaded, a switch changes or a workflow is loaded.
//
// Stepping for "Load Image + Embedded Prompt": right after each queue
// (ComfyUI's afterQueued hook, the same one seeds use) the node's own
// image / variation widgets are moved on, so the image preview, the image
// path and the prompt shown are always what the NEXT queue renders.
//   next_image      next image in the same folder (wraps), variation kept
//   next_variation  next prompt this image has (wraps)
//   both            every prompt of the image, then the next image,
//                   starting again at its first prompt
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

const NODES = new Set(["ImgPromptLoader", "ImgPromptBatch"]);
const WATCH = ["image", "variation", "next_image", "next_variation", "include_base"];

// ---------------------------------------------------------------- stepping --
function dirOf(name) {
  const i = String(name).lastIndexOf("/");
  return i < 0 ? "" : name.slice(0, i);
}

// prompt keys an image has: base, 1..n (base dropped if excluded)
export function promptKeys(count, includeBase) {
  const keys = ["base"];
  for (let i = 1; i <= (count || 0); i++) keys.push(String(i));
  return includeBase || keys.length === 1 ? keys : keys.slice(1);
}

// -> {image, variation} for the next queue
export function nextState(s) {
  const { image, variation, nextImage, nextVariation, includeBase, files, counts } = s;
  if (!nextImage && !nextVariation) return { image, variation };
  if (nextVariation) {
    const keys = promptKeys(counts[image], includeBase);
    const i = keys.indexOf(variation);
    if (i + 1 < keys.length) return { image, variation: keys[i + 1] };
    if (!nextImage) return { image, variation: keys[0] };       // wrap
  }
  const sibs = files.filter((f) => dirOf(f) === dirOf(image));
  const nextImg = sibs.length ? sibs[(sibs.indexOf(image) + 1) % sibs.length] : image;
  return {
    image: nextImg,
    variation: nextVariation ? promptKeys(counts[nextImg], includeBase)[0] : variation,
  };
}

// --------------------------------------------------------------- display --
function format(meta, selected) {
  if (!meta) return "(no embedded prompt in this image)";
  const rows = [];
  const b = meta.base || {};
  rows.push(["base", "base", b.positive || "", b.negative || "", ""]);
  (meta.variations || []).forEach((v, i) =>
    rows.push([String(i + 1), v.label || `variation ${i + 1}`,
               v.positive || "", v.negative || "", v.change || ""]));
  const tm = meta.template ? `  [${meta.template}]` : "";
  const ag = (meta.ages || []).length ? `  age ${meta.ages.join("/")}` : "";
  const from = meta.orig_name ? `from: ${meta.folder || meta.char || ""}/${meta.orig_name}${tm}${ag}\n\n` : "";
  return from + rows.map(([key, label, pos, neg, change]) => {
    const mark = selected === key ? ">> " : "";
    return `${mark}[${key}] ${label}${change ? " - " + change : ""}\n+ ${pos}\n- ${neg}`;
  }).join("\n\n");
}

function widget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function val(node, name) {
  return widget(node, name)?.value;
}

function signature(node) {
  return WATCH.map((n) => String(val(node, n))).join("|");
}

// variation counts for every image in the current image's folder, kept on
// the node so stepping after a queue never waits on the server
async function loadFolder(node, image, force = false) {
  const dir = dirOf(image);
  if (!force && node.__ipFolder?.dir === dir) return;
  try {
    const j = await (await api.fetchApi(`/imgprompt/folder?image=${encodeURIComponent(image)}`)).json();
    if (j.ok) node.__ipFolder = { dir, counts: j.counts || {} };
  } catch (e) {
    console.warn("[imgprompt] folder lookup failed", e);
  }
}

async function refresh(node) {
  const box = node.__ipBox;
  if (!box) return;
  node.__ipSig = signature(node);
  const image = val(node, "image");
  if (!image) { box.value = ""; return; }
  try {
    const j = await (await api.fetchApi(`/imgprompt/meta?image=${encodeURIComponent(image)}`)).json();
    if (j.error) { box.value = `(error: ${j.error})`; return; }
    if (node.comfyClass === "ImgPromptLoader") {
      const variation = val(node, "variation") ?? "base";
      const stepping = val(node, "next_image") || val(node, "next_variation");
      const head = [
        node.__ipRendered || "",
        stepping ? `next queue: ${image}  [${variation}]` : "",
      ].filter(Boolean).join("\n");
      box.value = (head ? head + "\n\n" : "") + format(j.meta, variation);
      if (stepping) await loadFolder(node, image);
    } else {
      box.value = format(j.meta);
    }
  } catch (e) {
    box.value = `(couldn't read metadata: ${e})`;
  }
  node.setDirtyCanvas(true, true);
}

function step(node) {
  const imgW = widget(node, "image");
  const varW = widget(node, "variation");
  if (!imgW || !varW) return;
  const nextImage = !!val(node, "next_image");
  const nextVariation = !!val(node, "next_variation");
  if (!nextImage && !nextVariation) return;
  const next = nextState({
    image: imgW.value, variation: varW.value, nextImage, nextVariation,
    includeBase: val(node, "include_base") !== false,
    files: imgW.options?.values || [],
    counts: node.__ipFolder?.dir === dirOf(imgW.value) ? node.__ipFolder.counts : {},
  });
  if (next.variation !== varW.value) {
    varW.value = next.variation;
    varW.callback?.(next.variation);
  }
  if (next.image !== imgW.value) {
    imgW.value = next.image;
    imgW.callback?.(next.image);              // reloads the image preview
  }
  node.setDirtyCanvas(true, true);
}

app.registerExtension({
  name: "imgprompt.embeddedPrompt",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!NODES.has(nodeData.name)) return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);
      const w = ComfyWidgets["STRING"](this, "embedded_prompt",
        ["STRING", { multiline: true }], app).widget;
      w.serialize = false;
      if (w.inputEl) { w.inputEl.readOnly = true; w.inputEl.style.opacity = 0.85; }
      this.__ipBox = w;
      if (nodeData.name === "ImgPromptLoader") {
        // runs once per queued prompt, after it was sent (batch count too)
        w.afterQueued = () => step(this);
        // re-read variation counts in case images were re-tagged meanwhile
        w.beforeQueued = () => {
          if (val(this, "next_image") || val(this, "next_variation")) loadFolder(this, val(this, "image"), true);
        };
      }
      setTimeout(() => refresh(this), 50);
      return r;
    };

    // Catches dropdown changes, toggles, stepping, uploads and workflow loads.
    const onDraw = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function () {
      const r = onDraw?.apply(this, arguments);
      if (this.__ipBox && signature(this) !== this.__ipSig) refresh(this);
      return r;
    };

    const onExec = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (msg) {
      onExec?.apply(this, arguments);
      const text = msg?.text?.join("\n") || "";
      if (this.comfyClass === "ImgPromptLoader" && text.startsWith("rendered:")) {
        this.__ipRendered = text.split("\n", 1)[0];
        refresh(this);
      } else if (text && this.__ipBox) {
        this.__ipBox.value = text;
      }
    };
  },
});
