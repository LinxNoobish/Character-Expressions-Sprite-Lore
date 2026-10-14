"""
Vision Caption + Orientation Latent  -  ComfyUI custom nodes (any image model)
Installed by the Sprite Lore installer as custom_nodes/comfyui-vlcaption/__init__.py

Nodes:
  * Qwen VL Caption       - captions an image into a text-to-image prompt with a
                            local Qwen2.5-VL / Qwen3-VL model (transformers)
  * Orientation Latent    - picks landscape/portrait/square from the input
                            image, scales the image to that layout (optionally
                            through an upscale model) and returns it as an
                            img2img latent, or an empty latent when toggled

Workflows saved with the earlier single-file version (node ids
QwenVLCaptionZIT / OrientationLatentZIT) still load: those ids are kept as
hidden aliases of the same nodes.
"""

import gc
import math
import os
import re

import numpy as np
import torch
from PIL import Image

import comfy.model_management as mm
import comfy.utils
import folder_paths

# --------------------------------------------------------------------------------------
# Qwen VL captioner
# --------------------------------------------------------------------------------------

LLM_DIR = os.path.join(folder_paths.models_dir, "LLM")

HF_PRESETS = [
    "Qwen/Qwen2.5-VL-7B-Instruct",
    "Qwen/Qwen2.5-VL-3B-Instruct",
    "Qwen/Qwen3-VL-8B-Instruct",
    "Qwen/Qwen3-VL-4B-Instruct",
]

DEFAULT_INSTRUCTION = (
    "Describe this image as a single detailed paragraph to be used as a prompt for a "
    "text-to-image model. Cover the main subject, their appearance, clothing, pose, "
    "facial expression, the setting and background, lighting, color palette, camera "
    "angle and framing, and the overall art style or medium. Write plain descriptive "
    "prose. Do not use lists, do not add a preamble, and do not mention that this is "
    "an image or a photo description."
)

_CACHE = {"key": None, "model": None, "processor": None}


def _local_models():
    if not os.path.isdir(LLM_DIR):
        return []
    out = []
    for name in sorted(os.listdir(LLM_DIR)):
        p = os.path.join(LLM_DIR, name)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "config.json")):
            out.append("local:" + name)
    return out


def _resolve_model_path(choice):
    if choice.startswith("local:"):
        return os.path.join(LLM_DIR, choice[len("local:"):])
    local_dir = os.path.join(LLM_DIR, choice.split("/")[-1])
    if not os.path.isfile(os.path.join(local_dir, "config.json")):
        from huggingface_hub import snapshot_download

        os.makedirs(LLM_DIR, exist_ok=True)
        print(f"[VL Caption] Downloading {choice} -> {local_dir}")
        snapshot_download(repo_id=choice, local_dir=local_dir)
    return local_dir


def _free_captioner():
    _CACHE["model"] = None
    _CACHE["processor"] = None
    _CACHE["key"] = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _tensor_to_pil(image, max_side=1280):
    arr = (image[0].cpu().numpy().clip(0, 1) * 255).astype(np.uint8)
    pil = Image.fromarray(arr).convert("RGB")
    w, h = pil.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        pil = pil.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return pil


class QwenVLCaption:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "model": (HF_PRESETS + _local_models(), {"default": HF_PRESETS[0]}),
                "instruction": ("STRING", {"multiline": True, "default": DEFAULT_INSTRUCTION}),
                "prefix": ("STRING", {"multiline": True, "default": ""}),
                "suffix": ("STRING", {"multiline": True, "default": ""}),
                "max_new_tokens": ("INT", {"default": 400, "min": 32, "max": 2048, "step": 8}),
                "temperature": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                          "tooltip": "0 = greedy / deterministic"}),
                "device": (["auto", "cuda:0", "cuda:1", "cpu"], {"default": "auto"}),
                "dtype": (["bfloat16", "float16", "float32"], {"default": "bfloat16"}),
                "unload_after": ("BOOLEAN", {"default": True,
                                             "tooltip": "Free the VL model's VRAM before the image model loads"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "caption"
    CATEGORY = "image/vision"

    def caption(self, image, model, instruction, prefix, suffix, max_new_tokens,
                temperature, device, dtype, unload_after):
        from transformers import AutoModelForImageTextToText, AutoProcessor

        dev = mm.get_torch_device() if device == "auto" else torch.device(device)
        tdtype = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}[dtype]
        if dev.type == "cpu" and tdtype != torch.float32:
            tdtype = torch.float32

        key = (model, str(dev), dtype)
        if _CACHE["key"] != key:
            _free_captioner()
            path = _resolve_model_path(model)
            print(f"[VL Caption] Loading {path} on {dev} ({tdtype})")
            processor = AutoProcessor.from_pretrained(path)
            vl = AutoModelForImageTextToText.from_pretrained(
                path, torch_dtype=tdtype, attn_implementation="sdpa"
            ).to(dev).eval()
            _CACHE.update(key=key, model=vl, processor=processor)

        vl, processor = _CACHE["model"], _CACHE["processor"]
        pil = _tensor_to_pil(image)

        messages = [{"role": "user", "content": [
            {"type": "image"},
            {"type": "text", "text": instruction},
        ]}]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=[text], images=[pil], return_tensors="pt").to(dev)

        gen_kwargs = {"max_new_tokens": max_new_tokens}
        if temperature > 0:
            gen_kwargs.update(do_sample=True, temperature=temperature, top_p=0.9)
        else:
            gen_kwargs.update(do_sample=False)

        with torch.inference_mode():
            out = vl.generate(**inputs, **gen_kwargs)

        trimmed = out[:, inputs["input_ids"].shape[1]:]
        result = processor.batch_decode(trimmed, skip_special_tokens=True)[0]
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.S).strip()

        if unload_after:
            _free_captioner()

        parts = [p.strip() for p in (prefix, result, suffix) if p and p.strip()]
        final = " ".join(parts)
        print(f"[VL Caption] {final}")
        return (final,)


# --------------------------------------------------------------------------------------
# Orientation -> layout -> scaled reference -> latent
# --------------------------------------------------------------------------------------
#
# Scaling follows ComfyUI's own tools and the usual quality rules:
#   * crop/pad to the target aspect FIRST, at source resolution, so no detail is
#     spent on pixels that get thrown away
#   * shrinking:  "area" (box filter, averages every source pixel, no aliasing)
#   * enlarging:  an upscale model (ESRGAN family, via ComfyUI's own
#                 "Upscale Image (using Model)" code, tiled with OOM fallback) when
#                 one is wired in, then "area" down to the exact size because models
#                 only do fixed 2x/4x steps; without a model, "lanczos"
#   * img2img:    the scaled image goes through the VAE; empty_latent switches to a
#                 blank latent of the same size (txt2img) and sets denoise to 1.0

def _snap(v, m):
    return max(m, int(round(v / m)) * m)


def _resize(img, w, h, method):
    """IMAGE [B,H,W,C] -> [B,h,w,C] with comfy.utils.common_upscale."""
    if img.shape[2] == w and img.shape[1] == h:
        return img
    s = comfy.utils.common_upscale(img.movedim(-1, 1), w, h, method, "disabled")
    return s.movedim(1, -1).clamp(0.0, 1.0)


def _model_upscale(upscale_model, img):
    """Run ComfyUI's own ImageUpscaleWithModel so memory handling, tiling and
    OOM fallback match whatever ComfyUI version is installed."""
    from comfy_extras.nodes_upscale_model import ImageUpscaleWithModel as U
    if hasattr(U, "execute"):                # V3 node API (current ComfyUI)
        out = U.execute(upscale_model, img)
    else:                                    # older V1 node
        out = U().upscale(upscale_model, img)
    out = getattr(out, "result", out)
    return out[0]


def _scale_to(img, w, h, upscale_model, model_min_scale, down_method, up_method, steps):
    """Scale img to exactly w x h (aspect already matched by the caller)."""
    sh, sw = img.shape[1], img.shape[2]
    need = max(w / sw, h / sh)
    if need > 1.0 and upscale_model is not None and need >= model_min_scale:
        for _ in range(2):                   # a second pass only for tiny sources
            img = _model_upscale(upscale_model, img)
            steps.append(f"model {img.shape[2]}x{img.shape[1]}")
            if img.shape[2] >= w and img.shape[1] >= h:
                break
        sh, sw = img.shape[1], img.shape[2]
    if sw >= w and sh >= h:
        method = down_method
    else:
        method = up_method
    if (sw, sh) != (w, h):
        img = _resize(img, w, h, method)
        steps.append(f"{method} {w}x{h}")
    return img


def _center_crop(img, aspect):
    sh, sw = img.shape[1], img.shape[2]
    if abs(sw / sh - aspect) < 1e-4:
        return img
    if sw / sh > aspect:                     # too wide: trim the sides
        nw = max(1, round(sh * aspect))
        x = (sw - nw) // 2
        return img[:, :, x:x + nw, :]
    nh = max(1, round(sw / aspect))          # too tall: trim top and bottom
    y = (sh - nh) // 2
    return img[:, y:y + nh, :, :]


def _fill_transparent(img, mask, value=0.5):
    """MASK from LoadImage is 1 where the image was transparent. Fill those
    areas with neutral grey instead of whatever color sat under the alpha."""
    if mask is None:
        return img
    m = mask
    if m.ndim == 2:
        m = m.unsqueeze(0)
    if m.shape[-2:] != img.shape[1:3]:
        m = comfy.utils.common_upscale(m.unsqueeze(1), img.shape[2], img.shape[1],
                                       "bilinear", "disabled").squeeze(1)
    if m.shape[0] != img.shape[0]:
        m = m[:1].expand(img.shape[0], -1, -1)
    m = m.to(img.device, img.dtype).clamp(0, 1).unsqueeze(-1)
    return img[..., :3] * (1.0 - m) + value * m


class OrientationLatent:
    @classmethod
    def INPUT_TYPES(cls):
        # original inputs first, in their original order, so saved workflows load
        return {
            "required": {
                "image": ("IMAGE",),
                "mode": (["presets", "match_aspect"], {"default": "presets",
                         "tooltip": "presets: fixed W/H per orientation. match_aspect: keep source ratio at target megapixels"}),
                "landscape_width": ("INT", {"default": 1344, "min": 256, "max": 4096, "step": 16}),
                "landscape_height": ("INT", {"default": 768, "min": 256, "max": 4096, "step": 16}),
                "square_size": ("INT", {"default": 1024, "min": 256, "max": 4096, "step": 16}),
                "megapixels": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 4.0, "step": 0.05}),
                "square_tolerance": ("FLOAT", {"default": 0.05, "min": 0.0, "max": 0.5, "step": 0.01,
                                     "tooltip": "Aspect ratios within this of 1.0 count as square"}),
                "force": (["auto", "landscape", "portrait", "square"], {"default": "auto"}),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 64}),
                "empty_latent": ("BOOLEAN", {"default": False, "label_on": "empty (txt2img)",
                                 "label_off": "reference image (img2img)",
                                 "tooltip": "Off: the scaled reference goes through the VAE. On: blank latent of the same size, denoise output 1.0"}),
                "img2img_denoise": ("FLOAT", {"default": 0.60, "min": 0.0, "max": 1.0, "step": 0.01,
                                    "tooltip": "Sent out on 'denoise' when using the reference. ~0.2 polish, 0.3-0.45 restyle, 0.5-0.65 strong change (anime -> photo)"}),
                "fit": (["crop", "pad", "stretch"], {"default": "crop",
                        "tooltip": "How the reference meets the layout's aspect: crop the overflow (centered), pad with grey, or stretch"}),
                "downscale_method": (["area", "lanczos", "bicubic", "bilinear"], {"default": "area",
                                     "tooltip": "Used whenever the image gets smaller (also after an upscale model). area = box filter, no aliasing"}),
                "upscale_method": (["lanczos", "bicubic", "bilinear"], {"default": "lanczos",
                                   "tooltip": "Used to enlarge when no upscale model is used"}),
                "model_min_scale": ("FLOAT", {"default": 1.25, "min": 1.0, "max": 8.0, "step": 0.05,
                                    "tooltip": "Only run the upscale model when the image must grow at least this much"}),
            },
            "optional": {
                "vae": ("VAE", {"tooltip": "Needed for the reference latent; also sets the empty latent's channels"}),
                "upscale_model": ("UPSCALE_MODEL", {"tooltip": "From 'Load Upscale Model'. Used to enlarge small references"}),
                "mask": ("MASK", {"tooltip": "Alpha mask from the loader: transparent areas become neutral grey"}),
            },
        }

    # original outputs first, new ones appended, so existing links stay valid
    RETURN_TYPES = ("LATENT", "INT", "INT", "STRING", "IMAGE", "FLOAT")
    RETURN_NAMES = ("latent", "width", "height", "orientation", "image", "denoise")
    FUNCTION = "build"
    CATEGORY = "latent/layout"

    def build(self, image, mode, landscape_width, landscape_height, square_size,
              megapixels, square_tolerance, force, batch_size, empty_latent=False,
              img2img_denoise=0.6, fit="crop", downscale_method="area",
              upscale_method="lanczos", model_min_scale=1.25, vae=None,
              upscale_model=None, mask=None):
        src_h, src_w = int(image.shape[1]), int(image.shape[2])
        ratio = src_w / src_h

        # ---- layout (unchanged logic) ------------------------------------------------
        if force != "auto":
            orient = force
        elif abs(ratio - 1.0) <= square_tolerance:
            orient = "square"
        elif ratio > 1.0:
            orient = "landscape"
        else:
            orient = "portrait"

        if mode == "presets":
            long_side = max(landscape_width, landscape_height)
            short_side = min(landscape_width, landscape_height)
            if orient == "landscape":
                w, h = long_side, short_side
            elif orient == "portrait":
                w, h = short_side, long_side
            else:
                w = h = square_size
        else:
            r = 1.0 if orient == "square" else ratio
            if force == "landscape" and r < 1.0:
                r = 1.0 / r
            if force == "portrait" and r > 1.0:
                r = 1.0 / r
            total = megapixels * 1_000_000
            h = math.sqrt(total / r)
            w = h * r

        # latent geometry from the VAE when one is connected (Z-Image / Flux VAE: 16ch, /8)
        if vae is not None:
            try:
                ratio_px = int(vae.spacial_compression_encode())
            except Exception:
                ratio_px = 8
            ratio_px = ratio_px if ratio_px > 0 else 8
            channels = int(getattr(vae, "latent_channels", 16))
        else:
            ratio_px, channels = 8, 16
        grid = max(16, ratio_px)
        w, h = _snap(w, grid), _snap(h, grid)

        # ---- scale the reference into the layout --------------------------------------
        steps = []
        img = _fill_transparent(image[..., :3], mask)
        if mask is not None:
            steps.append("alpha->grey")
        if fit == "crop":
            img = _center_crop(img, w / h)
            if img.shape[1:3] != image.shape[1:3]:
                steps.append(f"crop {img.shape[2]}x{img.shape[1]}")
            img = _scale_to(img, w, h, upscale_model, model_min_scale,
                            downscale_method, upscale_method, steps)
        elif fit == "pad":
            s = min(w / src_w, h / src_h)
            iw, ih = min(w, max(1, round(src_w * s))), min(h, max(1, round(src_h * s)))
            inner = _scale_to(img, iw, ih, upscale_model, model_min_scale,
                              downscale_method, upscale_method, steps)
            canvas = torch.full((img.shape[0], h, w, 3), 0.5, dtype=inner.dtype, device=inner.device)
            y, x = (h - ih) // 2, (w - iw) // 2
            canvas[:, y:y + ih, x:x + iw, :] = inner
            img = canvas
            if (iw, ih) != (w, h):
                steps.append(f"pad to {w}x{h}")
        else:
            img = _scale_to(img, w, h, upscale_model, model_min_scale,
                            downscale_method, upscale_method, steps)
        img = img.to(mm.intermediate_device())

        # ---- latent --------------------------------------------------------------------
        if empty_latent:
            samples = torch.zeros([batch_size, channels, h // ratio_px, w // ratio_px],
                                  device=mm.intermediate_device())
            latent = {"samples": samples, "downscale_ratio_spacial": ratio_px}
            denoise, src = 1.0, "empty latent"
        else:
            if vae is None:
                raise ValueError("Orientation Latent: connect a VAE to use the reference image "
                                 "as the latent, or switch empty_latent on")
            samples = vae.encode(img)
            if batch_size > samples.shape[0]:
                samples = comfy.utils.repeat_to_batch_size(samples, batch_size)
            latent = {"samples": samples}
            denoise, src = float(img2img_denoise), f"img2img denoise {img2img_denoise:.2f}"

        info = (f"{orient} {w}x{h} (source {src_w}x{src_h}, ratio {ratio:.3f}) | "
                f"{fit}: {' -> '.join(steps) or 'no resize'} | {src}")
        print(f"[Orientation] {info}")
        return (latent, w, h, info, img, denoise)


class QwenVLCaptionZIT(QwenVLCaption):
    """Old node id, kept so saved workflows load; hidden from search."""
    DEPRECATED = True


class OrientationLatentZIT(OrientationLatent):
    """Old node id, kept so saved workflows load; hidden from search."""
    DEPRECATED = True


NODE_CLASS_MAPPINGS = {
    "QwenVLCaption": QwenVLCaption,
    "OrientationLatent": OrientationLatent,
    "QwenVLCaptionZIT": QwenVLCaptionZIT,
    "OrientationLatentZIT": OrientationLatentZIT,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "QwenVLCaption": "Qwen VL Caption",
    "OrientationLatent": "Orientation Latent",
    "QwenVLCaptionZIT": "Qwen VL Caption (old id)",
    "OrientationLatentZIT": "Orientation Latent (old id)",
}
