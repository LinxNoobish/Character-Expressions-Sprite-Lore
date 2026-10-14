# Sprite Lore

Lore and image prompts that live inside your SillyTavern expression sprites.

Every sprite PNG can carry a small JSON block (PNG text chunk `st_expr`) with
vision tags, a lore entry written for that exact emotion and degree, extra
entries you add yourself, and anime2img image prompts. The pixels are never
touched. SillyTavern then sends the lore of whichever sprite is on screen.

| Part | What it does |
|---|---|
| **Sprite Lore page** | Browse sprite folders, tag images with a vision model, write lore with your own templates, emotion degrees and directions, tags and per-character instructions, rename sprites, create image prompts. Runs on its own at `http://127.0.0.1:5181/`, and optionally inside CharacterVault. |
| **SillyTavern extension** | Reads the embedded lore of the sprites on screen and injects it into the prompt, with adjustable detail, the next stronger/weaker degree, your own fields and your own template. |
| **ComfyUI nodes** | *Load Image + Embedded Prompt* / *All Prompts* / *Save Image (Prompt Only)* for images prompted by anime2img; *Qwen VL Caption* and *Orientation Latent* for image-to-image with any model. |
| **anime2img** | Turns a sprite into a photo-style prompt with a vision model; bundled with two templates (`exact`, `creative`). |
| **Command-line tools** | `tag_sprites.py`, `write_lore.py`, `batch_runner.py` for whole libraries. |

## Requirements

- Python 3.9 or newer. Pillow is installed automatically if missing (into a
  private venv, your system Python is left alone).
- An OpenAI-compatible backend: KoboldCpp, llama.cpp server, LM Studio, etc.
  Tagging and image prompts need a model with image input (e.g. Qwen2.5-VL /
  Qwen3-VL with its mmproj). Lore can use the same or a separate text model.
- SillyTavern (any recent release) for the extension.
- ComfyUI (optional) for the nodes. *Qwen VL Caption* needs `transformers`
  and `huggingface_hub` in ComfyUI's Python.
- CharacterVault (optional) if you want the page inside it.

## Install

1. Unzip everything into one folder.
2. Optional: if SillyTavern or ComfyUI are not in a standard place, or your
   backend isn't on port 5001, copy the lines you need from `config.ini` into
   a new `config.local.ini` next to it and fill them in, e.g.

   ```ini
   [paths]
   sillytavern = /data/apps/SillyTavern
   comfyui = /data/apps/ComfyUI
   [backends]
   vision_url = http://127.0.0.1:5001/v1
   lore_url = http://127.0.0.1:5005/v1
   ```
3. Run `install.sh` (Linux/macOS) or `install.bat` (Windows).
   `install.sh --dry-run` shows what it found without changing anything.

The installer prints every path it found and how (config / last install /
found). Anything it couldn't find is reported with the config key to set.
Re-running it updates the code and keeps your settings, profiles, character
guides and your own anime2img templates.

### What goes where

| From the zip | Installed to |
|---|---|
| `spritelore_*.py`, `exprlore_common.py`, `spritelore_ui.html`, `spritelore_defaults.json`, `spritelore_template_history.json`, CLI tools | `install_dir` (default `~/.local/share/spritelore`) |
| `anime2img.py`, `anime2img_*.txt` | `install_dir/anime2img` (templates only if not already there) |
| `st_spritelore_index.js`, `st_spritelore_manifest.json`, `st_spritelore_style.css` | `SillyTavern/public/scripts/extensions/third-party/SpriteLore/` as `index.js`, `manifest.json`, `style.css` |
| `comfy_imgprompt_nodes.py`, `comfy_imgprompt.js` | `ComfyUI/custom_nodes/comfyui-imgprompt/__init__.py`, `js/imgprompt.js` |
| `comfy_vlcaption_*` | `ComfyUI/custom_nodes/comfyui-vlcaption/` (`__init__.py`, `requirements.txt`, `README.md`, `example_workflows/`) and the example into ComfyUI's workflow list |
| `SpriteLore*.tsx`, `SpriteLoreApi.ts` | `CharacterVault/src/pages/sprite-lore/` + a route and header button (only if `charactervault` is set or found) |

Older copies of the ComfyUI nodes that register the same node ids (for example
`custom_nodes/zit_caption_nodes.py`) are renamed to `*.disabled` so nothing
loads twice. Workflows that use the old ids `QwenVLCaptionZIT` /
`OrientationLatentZIT` keep working.

On Linux with systemd, the server runs as the user service
`spritelore.service` and starts at login. Otherwise start it with
`start_spritelore.sh` / `start_spritelore.bat` in `install_dir`.

### Config keys

All keys are optional. Saved after install to
`~/.config/spritelore/spritelore_local.json` (override with the
`SPRITELORE_CONFIG` environment variable); the server, anime2img and the CLI
tools read that file.

| Key | Meaning | Default |
|---|---|---|
| `install_dir` | server + data | `~/.local/share/spritelore` |
| `sillytavern` | SillyTavern folder (has `server.js`) | auto-detected |
| `st_user` | user folder under `data/` | `default-user` |
| `comfyui` | ComfyUI folder (has `main.py`, `custom_nodes/`) | auto-detected |
| `comfy_input` | where prompted copies go for ComfyUI | `<comfyui>/input` |
| `charactervault` | CharacterVault checkout | none |
| `anime2img_dir` | your own anime2img folder | bundled copy |
| `failed_dir` | images anime2img couldn't prompt | `<anime2img_dir>/failed` |
| `sprite_roots` | extra sprite folders, `;`-separated | none |
| `vision_url`, `lore_url` | backend base URLs | `http://127.0.0.1:5001/v1` |
| `port` | Sprite Lore server port | `5181` |
| `service` | install the systemd user service | `yes` |
| `sillytavern_extension`, `comfyui_nodes`, `charactervault_page` | set `no` to skip a part | `yes` |

Auto-detection checks `~/SillyTavern`, `~/sillytavern`, `~/Documents/SillyTavern`,
`~/SillyTavern-Launcher/SillyTavern`, `/opt/SillyTavern` (and the ComfyUI
equivalents), then looks up to three folders deep under your home folder. It
never searches outside your home folder.

## Using it

Open `http://127.0.0.1:5181/` (or **Sprite Lore** in CharacterVault's header).

1. **Pick a character folder** on the left. SillyTavern's characters folder
   and every `sprite_roots` folder are listed.
2. **Tag** (`VLM tag untagged`): the vision model describes each sprite and
   suggests an emotion name. Accept suggested names all at once, or tick
   *Auto-accept lore + names*. To reject them, *Keep current names* (or the
   × on a card's "rename →" badge) keeps the file names and stops the
   highlighting; the card shows "name kept" (click it to see the suggestion
   again). The suggestion comes back by itself only if you rename the file or
   a re-tag gives a different answer. Rename anything by hand with the pencil
   or F2.
3. **Steer the character**: tick tags (lore focus, tone, bond with
   {{user}}, world, your own), write a custom instruction and notes. They are
   saved per character and used for every future generation.
4. **Generate lore**: each sprite gets an entry tied to its emotion, its
   degree (when a label has several images, they get different degrees,
   faint → overwhelming) and its target (self / {{user}} / the world).
   Entries are written to set up the character's next turn: last response →
   this emotion → how the next reply reacts.
5. **Image prompts**: pick a template (`exact`, `creative` or your own) and
   run; copies with the prompt embedded go to ComfyUI's input folder for the
   *Load Image + Embedded Prompt* node.

Settings (top right) hold backends, lore templates, the degree ladder,
directions, emotion rules and tag lists. Every section has a reset to the
shipped defaults, the previous save can be restored, and **profiles** keep
separate setups for different kinds of characters. When an update ships new
default templates, *Review upgrade* merges the new wording into templates you
edited, and shows conflicts for you to pick.

## SillyTavern extension

Extensions → **Sprite Lore**:

- **Send**: lore only / tags only / all information.
- **Lore detail** 1–5: lore → + scene keywords → + extra entries →
  + degree and target → + visual tags and description.
- **Emotion ladder**: also send the lore of the next stronger and/or weaker
  sprite of the same emotion, as "If it escalates" / "If it cools down", so
  the model can move the character along the scale.
- **Degree order**: the names of your degrees, mildest first (matches the
  page's ladder).
- **Custom fields**: any value in the sprite's JSON, one per line as
  `Label = path`, e.g. `Trigger = lore.trigger`, `First entry = entries.0.text`.
- **Use my own template**: write the whole injection yourself with
  `{{sl_char}} {{sl_emotion}} {{sl_lore}} {{sl_keywords}} {{sl_degree}}
  {{sl_target}} {{sl_tags}} {{sl_description}} {{sl_entries}}
  {{sl_stronger}} {{sl_weaker}}`, `{{sl_entry::Title}}` (an extra entry by
  title) and `{{sl_field::path}}` (any JSON value). A line whose macros are all
  empty is left out. Normal SillyTavern macros (`{{user}}`, `{{char}}`) work.
- **Position / depth / role**, **World Info scan**, **max characters**.
- **Sprites on screen**: untick a character to keep its sprites out of the
  prompt (useful in groups).
- `{{spritelore}}` macro: the current text, for use in your own prompts even
  with injection off.

## Command-line tools

Run from `install_dir` (the backend URLs default to your config):

```sh
python3 tag_sprites.py  ~/SillyTavern/data/default-user/characters/Tessa --dry-run
python3 write_lore.py   ~/SillyTavern/data/default-user/characters/Tessa
python3 batch_runner.py status
python3 anime2img/anime2img.py --help
```

## Updating and removing

- Update: unzip the new version, keep your `config.local.ini`, run the
  installer again.
- Remove: `uninstall.sh` here, or `uninstall_spritelore.sh` in the install
  folder (`.bat` on Windows). It removes only what the installer placed, as
  recorded in `~/.config/spritelore/install_manifest.json`:
  - every file it copied or wrote, and the folders it created if they end up
    empty;
  - a file that was already there before the first install (an older
    extension, an older `comfyui-imgprompt`) is put back from its
    `<file>.spritelore-bak` copy; `--no-restore` deletes it instead;
  - a file you edited after it was installed is left alone and listed;
  - old ComfyUI nodes it renamed to `*.disabled` get their names back;
  - in CharacterVault only the added route/import/button lines are removed;
  - the service is stopped and disabled.

  Your settings, profiles, guides, rename logs, sprites and the lore inside
  them are never touched. `--dry-run` lists everything first.

## Embedded data (`st_expr`)

PNG `tEXt` chunk, keyword `st_expr`, value = base64 of UTF-8 JSON:

```json
{
  "v": 1,
  "pxhash": "sha256 of the image data (IDAT), unchanged by metadata edits",
  "orig_name": "anger-2.png",
  "label": "anger",
  "vlm": { "emotion": "anger", "confidence": 0.9,
           "categories": { "appearance": [], "expression": [], "action": [], "objects": [], "setting": [] },
           "description": "..." },
  "plan": { "intensity": "strong", "direction": "user" },
  "lore": { "text": "...", "keywords": ["..."], "intensity": "strong", "direction": "user",
            "template": "grounded", "reviewed": false, "stale": false },
  "entries": [ { "title": "Voice", "text": "...", "keywords": [] } ],
  "keepName": { "label": "joy", "rejected": "anger", "ts": "..." },
  "imgprompts": { "exact": { "out": "...", "base": { "positive": "...", "negative": "..." } } }
}
```

Prompted copies made for ComfyUI also carry an `imgprompt` iTXt chunk and an
A1111-style `parameters` chunk.

## Troubleshooting

- **Page says the server isn't running**: `systemctl --user status spritelore`
  or `journalctl --user -u spritelore -n 30`; or run `start_spritelore.sh` to
  see errors directly.
- **"not a vision model"** when tagging: load the model's mmproj in your
  backend, or point `vision_url` at the backend that has it.
- **Image prompts can't find ComfyUI**: set `comfyui` (or `comfy_input`) in
  `config.local.ini` and re-run the installer.
- **Extension shows nothing**: the sprite must have lore (or tags, with the
  tags fallback on) and be visible on screen; *Re-read sprites* forces a
  rescan. The preview box shows exactly what is sent.

## License

GPL-3.0 (see `LICENSE`). The page is built on CharacterVault's UI and theme,
which are GPL-3.0.
