# Qwen VL Caption + Orientation Latent (ComfyUI nodes)

Two nodes for image-to-image work with any image model. Installed as
`ComfyUI/custom_nodes/comfyui-vlcaption/`. The example workflow
(`vlcaption_example_zimage_turbo.json`, also copied to your ComfyUI workflows
list) happens to use Z-Image Turbo; swap the model loaders for your own.

Workflows saved with the earlier single-file version (`zit_caption_nodes.py`,
node ids `QwenVLCaptionZIT` / `OrientationLatentZIT`) still load: the old ids
are kept as hidden aliases. The installer renames an old
`custom_nodes/zit_caption_nodes.py` to `zit_caption_nodes.py.disabled` so the
two don't register the same nodes twice.

## Pipeline (example)
Load Image → **Qwen VL Caption** → CLIPTextEncode (positive) → ConditioningZeroOut (negative)
Load Image → **Orientation Latent** (+ VAE) → KSampler latent, and its `denoise` output → KSampler denoise

## Requirements (ComfyUI's Python)
- `transformers` ≥ 4.49 for Qwen2.5-VL, ≥ 4.57 for Qwen3-VL
- `huggingface_hub`

The installer checks for them and prints the exact pip command for your
ComfyUI environment if they're missing; it does not change that environment
itself.

## Qwen VL Caption
- `model`: a Hugging Face repo (downloaded once into `models/LLM/<repo-name>`)
  or any model folder already in `models/LLM/` (shown as `local:<name>`).
- `instruction`, `prefix`, `suffix`: what the caption asks for and what is
  put around it.
- `unload_after` (default on) frees the caption model before the image model
  loads; `device` can pin it to a second GPU (`cuda:1`).

## Orientation Latent

### Layout
- ratio = src_w / src_h. |ratio − 1| ≤ `square_tolerance` → square; > 1 → landscape; else portrait.
- `presets`: landscape = landscape_width×landscape_height, portrait = swapped, square = square_size².
- `match_aspect`: keeps the source ratio at `megapixels`.
- Sizes snap to multiples of 16 (or the VAE's downscale factor if larger). `force` overrides detection.

### Scaling the reference into the layout
1. `mask` connected: transparent areas become neutral grey (sprites otherwise turn black under the alpha).
2. `fit`
   - `crop` (default): centre-crop to the layout's aspect at source resolution, then scale.
   - `pad`: scale to fit inside, grey bars fill the rest.
   - `stretch`: scale straight to the layout size.
3. Enlarging: if `upscale_model` is connected and the image must grow ≥ `model_min_scale`, ComfyUI's own
   Upscale Image (using Model) runs (tiled, OOM fallback; a second pass only for tiny sources), then
   `downscale_method` brings it to the exact size. Without a model: `upscale_method` (lanczos).
4. Shrinking: `downscale_method` (`area` = box filter, averages all source pixels, no aliasing).

### Latent
- `empty_latent` off (default): the scaled image goes through the VAE → img2img latent; the `denoise`
  output carries `img2img_denoise` (0.60). Needs the `vae` input.
- `empty_latent` on: blank latent of the same size (channels from the VAE if connected, else 16);
  `denoise` output is 1.0. The scaled image is still on the `image` output (IPAdapter / preview).
- Denoise guide (Z-Image Turbo img2img; other models differ): ~0.15–0.25 polish, 0.30–0.45 restyle,
  0.50–0.65 strong change. Anime → photoreal needs the strong range; above ~0.35 faces drift.

### Outputs
`latent, width, height, orientation (log line), image (scaled reference), denoise`

## Models used by the example workflow
- `models/diffusion_models/z_image_turbo_bf16.safetensors`
- `models/text_encoders/qwen_3_4b.safetensors` (CLIPLoader type `lumina2`)
- `models/vae/ae.safetensors`
- Optional: any ESRGAN-family model in `models/upscale_models/` (Load Upscale Model → `upscale_model`)
