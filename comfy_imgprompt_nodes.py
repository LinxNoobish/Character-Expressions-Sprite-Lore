"""
ComfyUI nodes for images tagged by anime2img.py.

  Load Image + Embedded Prompt   image, mask, positive, negative, label for
                                 the chosen variation (base or 1..8), with
                                 switches to step through the folder's
                                 images and/or each image's prompts
  Load Image + All Prompts       same image, plus lists of every prompt so
                                 one queue renders base + all variations
  Save Image (Prompt Only)       saves PNGs with just the prompt text, no
                                 workflow embedded

Stepping (Load Image + Embedded Prompt), done in js/imgprompt.js right
after each queue, by changing the node's own widgets (so the image preview
and the image path always show what the next queue renders):
  next_image       next image in the same folder (wraps); variation kept
  next_variation   next prompt this image actually has (wraps)
  both on          every prompt of the image, then the next image starting
                   again at its first prompt
  include_base     the base prompt is part of the prompt cycle

The embedded text shows on the node as soon as anything changes (served by
the routes below).

Installed by the Sprite Lore installer as custom_nodes/comfyui-imgprompt/__init__.py
(js/imgprompt.js next to it).
"""

import hashlib
import json
import os
import struct
import zlib

import numpy as np
import torch
from PIL import Image, ImageOps, ImageSequence

import folder_paths

META_KEY = "imgprompt"
SUBDIR = "imgprompt"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
VARIATION_CHOICES = ["base"] + [str(i) for i in range(1, 9)]
WEB_DIRECTORY = "./js"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------ helpers --
def list_images():
    inp = folder_paths.get_input_directory()
    out = []
    sub = os.path.join(inp, SUBDIR)
    if os.path.isdir(sub):
        found = []
        for d, subs, files in os.walk(sub):
            subs[:] = sorted(x for x in subs if not x.startswith("."))
            rel = os.path.relpath(d, inp).replace(os.sep, "/")
            found += [f"{rel}/{f}" for f in files if f.lower().endswith(IMG_EXT)]
        out += sorted(found)
    out += sorted(f for f in os.listdir(inp)
                  if os.path.isfile(os.path.join(inp, f))
                  and f.lower().endswith(IMG_EXT))
    return out


def image_path(name):
    path = folder_paths.get_annotated_filepath(name)
    inp = os.path.realpath(folder_paths.get_input_directory())
    real = os.path.realpath(path)
    if os.path.commonpath([inp, real]) != inp:
        raise ValueError(f"image outside the input folder: {name}")
    return path


def parse_a1111(text):
    pos, _, neg = text.partition("\nNegative prompt:")
    neg = neg.split("\nSteps:", 1)[0]
    return {"base": {"positive": pos.strip(), "negative": neg.strip()},
            "variations": [], "source": "parameters"}


def _png_texts(path):
    """Text chunks of a PNG, read from the chunk headers only (no pixel
    decoding), so scanning a whole folder stays fast."""
    out = {}
    with open(path, "rb") as f:
        if f.read(8) != PNG_SIG:
            return None
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            ln, typ = struct.unpack(">I4s", head)
            if typ in (b"tEXt", b"iTXt", b"zTXt"):
                body = f.read(ln)
                f.seek(4, 1)
                key, rest = body.split(b"\x00", 1)
                key = key.decode("latin-1", "replace")
                try:
                    if typ == b"tEXt":
                        out[key] = rest.decode("latin-1")
                    elif typ == b"zTXt":
                        out[key] = zlib.decompress(rest[1:]).decode("latin-1")
                    else:
                        flag, rest = rest[0], rest[2:]
                        _lang, rest = rest.split(b"\x00", 1)
                        _tkey, rest = rest.split(b"\x00", 1)
                        out[key] = (zlib.decompress(rest) if flag else rest).decode("utf-8")
                except Exception:
                    pass
            else:
                f.seek(ln + 4, 1)
            if typ == b"IEND":
                break
    return out


_META_CACHE = {}


def read_meta(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _META_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    info = None
    try:
        info = _png_texts(path)
        if info is None:                   # jpg / webp: let PIL find it
            with Image.open(path) as im:
                info = dict(im.info)
    except Exception:
        info = None
    meta = None
    if info:
        raw = info.get(META_KEY)
        if raw:
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                meta = None
        if meta is None and info.get("parameters"):
            meta = parse_a1111(str(info["parameters"]))
    _META_CACHE[path] = (key, meta)
    return meta


def entries(meta, include_base=True):
    """[(label, positive, negative, change)] in order base, 1..n."""
    out = []
    if meta:
        b = meta.get("base") or {}
        if include_base:
            out.append(("base", b.get("positive", ""), b.get("negative", ""), ""))
        for i, v in enumerate(meta.get("variations") or [], 1):
            out.append((v.get("label") or f"variation {i}", v.get("positive", ""),
                        v.get("negative", ""), v.get("change", "")))
    return out


def pick(meta, choice):
    if not meta:
        return "", "", "", "no embedded prompt"
    es = entries(meta)
    if choice != "base":
        idx = int(choice)
        if idx < len(es):
            return es[idx][:3] + ("",)
        return es[0][:3] + (f"variation {choice} not in image, used base",)
    return es[0][:3] + ("",)


def join(*parts):
    return ", ".join(p.strip().strip(",") for p in parts if p and p.strip())


def display(meta, selected=None):
    if not meta:
        return "(no embedded prompt in this image)"
    lines = []
    if meta.get("orig_name"):
        tm = f"  [{meta['template']}]" if meta.get("template") else ""
        ages = meta.get("ages") or []
        ag = f"  age {'/'.join(str(a) for a in ages)}" if ages else ""
        lines += [f"from: {meta.get('folder', meta.get('char', ''))}/{meta['orig_name']}{tm}{ag}", ""]
    for i, (label, pos, neg, change) in enumerate(entries(meta)):
        key = "base" if i == 0 else str(i)
        mark = ">> " if selected is not None and key == selected else ""
        head = f"{mark}[{key}] {label}" + (f" - {change}" if change else "")
        lines += [head, f"+ {pos}", f"- {neg}", ""]
    return "\n".join(lines).rstrip()


# ----------------------------------------------------------------- stepping --
def siblings(name):
    """Images in the same folder as `name`, in the dropdown's order."""
    folder = os.path.dirname(name)
    return [f for f in list_images() if os.path.dirname(f) == folder]


def variation_count(name):
    try:
        meta = read_meta(image_path(name))
    except Exception:
        meta = None
    return len((meta or {}).get("variations") or [])


def load_tensor(path):
    img = Image.open(path)
    frame = next(ImageSequence.Iterator(img))
    frame = ImageOps.exif_transpose(frame)
    if frame.mode == "I":
        frame = frame.point(lambda i: i * (1 / 255))
    rgb = frame.convert("RGB")
    image = torch.from_numpy(np.array(rgb).astype(np.float32) / 255.0)[None,]
    if "A" in frame.getbands():
        a = np.array(frame.getchannel("A")).astype(np.float32) / 255.0
        mask = 1.0 - torch.from_numpy(a)
    elif frame.mode == "P" and "transparency" in frame.info:
        a = np.array(frame.convert("RGBA").getchannel("A")).astype(np.float32) / 255.0
        mask = 1.0 - torch.from_numpy(a)
    else:
        mask = torch.zeros((rgb.height, rgb.width), dtype=torch.float32)
    return image, mask.unsqueeze(0)


def file_hash(name):
    h = hashlib.sha256()
    with open(image_path(name), "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def validate_image(image):
    if not folder_paths.exists_annotated_filepath(image):
        return f"Invalid image file: {image}"
    return True


# -------------------------------------------------------------------- nodes --
class ImgPromptLoader:
    CATEGORY = "image/imgprompt"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "mask", "positive", "negative", "label", "json", "filename")

    @classmethod
    def INPUT_TYPES(cls):
        # new inputs are appended at the end so saved workflows keep their
        # widget values in place
        return {
            "required": {
                "image": (list_images(), {"image_upload": True}),
                "variation": (VARIATION_CHOICES, {"default": "base"}),
            },
            "optional": {
                "positive_prefix": ("STRING", {"multiline": True, "default": ""}),
                "positive_suffix": ("STRING", {"multiline": True, "default": ""}),
                "negative_suffix": ("STRING", {"multiline": True, "default": ""}),
                "next_image": ("BOOLEAN", {"default": False, "tooltip":
                    "Each queue moves to the next image in the same folder"}),
                "next_variation": ("BOOLEAN", {"default": False, "tooltip":
                    "Each queue moves to the next prompt of the image. With "
                    "next_image also on: all prompts of an image, then the next image"}),
                "include_base": ("BOOLEAN", {"default": True, "tooltip":
                    "Stepping through prompts includes the base prompt"}),
            },
        }

    def load(self, image, variation, positive_prefix="", positive_suffix="",
             negative_suffix="", next_image=False, next_variation=False,
             include_base=True, **kw):
        # the stepping switches are applied by the frontend after each queue,
        # so image + variation are exactly what this run renders
        name, key = image, variation
        path = image_path(name)
        img, mask = load_tensor(path)
        meta = read_meta(path)
        label, pos, neg, miss = pick(meta, key)
        pos = join(positive_prefix, pos, positive_suffix)
        neg = join(neg, negative_suffix)
        head = f"rendered: {name}  [{key}]"
        text = head + "\n\n" + display(meta, key) + (
            f"\n\nnote: {miss}" if miss and meta else "")
        raw = json.dumps(meta, ensure_ascii=False) if meta else ""
        stem = os.path.splitext(os.path.basename(name))[0]
        return {"ui": {"text": [text]},
                "result": (img, mask, pos, neg, label, raw, stem)}

    @classmethod
    def IS_CHANGED(cls, image, variation="base", **kw):
        return f"{file_hash(image)}:{variation}"

    @classmethod
    def VALIDATE_INPUTS(cls, image, **kw):
        return validate_image(image)


class ImgPromptBatch:
    CATEGORY = "image/imgprompt"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "mask", "positives", "negatives", "labels")
    OUTPUT_IS_LIST = (False, False, True, True, True)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": (list_images(), {"image_upload": True}),
                "include_base": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "positive_prefix": ("STRING", {"multiline": True, "default": ""}),
                "positive_suffix": ("STRING", {"multiline": True, "default": ""}),
                "negative_suffix": ("STRING", {"multiline": True, "default": ""}),
            },
        }

    def load(self, image, include_base, positive_prefix="", positive_suffix="",
             negative_suffix=""):
        path = image_path(image)
        img, mask = load_tensor(path)
        meta = read_meta(path)
        es = entries(meta, include_base) or [("none", "", "", "")]
        pos = [join(positive_prefix, e[1], positive_suffix) for e in es]
        neg = [join(e[2], negative_suffix) for e in es]
        labels = [e[0] for e in es]
        return {"ui": {"text": [display(meta)]},
                "result": (img, mask, pos, neg, labels)}

    @classmethod
    def IS_CHANGED(cls, image, **kw):
        return file_hash(image)

    @classmethod
    def VALIDATE_INPUTS(cls, image, **kw):
        return validate_image(image)


class ImgPromptSave:
    """Saves PNGs that carry only the prompt text: no workflow, no graph.
    Stored as an A1111-style "parameters" chunk, which the loader nodes
    above (and most other tools) read back as the base prompt."""
    CATEGORY = "image/imgprompt"
    FUNCTION = "save"
    OUTPUT_NODE = True
    RETURN_TYPES = ()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "positive": ("STRING", {"forceInput": True}),
                "filename_prefix": ("STRING", {"default": "imgprompt"}),
                "save_to": (["output", "input/imgprompt"], {"default": "output"}),
                "include_negative": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "negative": ("STRING", {"forceInput": True}),
            },
        }

    def save(self, images, positive, filename_prefix, save_to,
             include_negative, negative=""):
        from PIL.PngImagePlugin import PngInfo
        if save_to == "output":
            base_dir, kind = folder_paths.get_output_directory(), "output"
        else:
            base_dir = os.path.join(folder_paths.get_input_directory(), SUBDIR)
            kind = "input"
            os.makedirs(base_dir, exist_ok=True)
        h, w = images[0].shape[0], images[0].shape[1]
        folder, fname, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, base_dir, w, h)
        if kind == "input":
            subfolder = os.path.join(SUBDIR, subfolder) if subfolder else SUBDIR
        text = positive.strip()
        if include_negative and negative and negative.strip():
            text += f"\nNegative prompt: {negative.strip()}"
        results = []
        for i, img in enumerate(images):
            arr = np.clip(255.0 * img.cpu().numpy(), 0, 255).astype(np.uint8)
            pil = Image.fromarray(arr)
            info = PngInfo()
            info.add_itxt("parameters", text)
            name = f"{fname.replace('%batch_num%', str(i))}_{counter:05}_.png"
            pil.save(os.path.join(folder, name), pnginfo=info, compress_level=4)
            results.append({"filename": name, "subfolder": subfolder, "type": kind})
            counter += 1
        return {"ui": {"images": results}}


# ------------------------------------------------------------------- routes --
try:
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.get("/imgprompt/meta")
    async def imgprompt_meta(request):
        name = request.rel_url.query.get("image", "")
        try:
            path = image_path(name)
            meta = read_meta(path) if os.path.isfile(path) else None
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e), "meta": None})
        return web.json_response({"ok": meta is not None, "meta": meta,
                                  "text": display(meta)})

    @PromptServer.instance.routes.get("/imgprompt/folder")
    async def imgprompt_folder(request):
        """Every image in the folder of `image`, with how many variations
        each has, so the frontend can step without waiting on the server."""
        name = request.rel_url.query.get("image", "")
        try:
            files = siblings(name)
            counts = {f: variation_count(f) for f in files}
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        return web.json_response({"ok": True, "images": files, "counts": counts})
except Exception as e:  # running outside ComfyUI
    print(f"[imgprompt] routes not registered: {e}")


NODE_CLASS_MAPPINGS = {
    "ImgPromptLoader": ImgPromptLoader,
    "ImgPromptBatch": ImgPromptBatch,
    "ImgPromptSave": ImgPromptSave,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "ImgPromptLoader": "Load Image + Embedded Prompt",
    "ImgPromptBatch": "Load Image + All Prompts",
    "ImgPromptSave": "Save Image (Prompt Only)",
}
