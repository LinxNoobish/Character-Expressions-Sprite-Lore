/*
 * Sprite Lore - reads the JSON embedded in expression sprites (PNG tEXt
 * chunk "st_expr", written by the Sprite Lore page, tag_sprites.py and
 * write_lore.py) and injects the lore, the visual tags, or both for the
 * sprite currently on screen.
 *
 * Detail level  1 lore . 2 + keywords . 3 + extra entries
 *               4 + degree and target . 5 + visual tags and description
 * Emotion ladder  also sends the lore of the next stronger and/or weaker
 *                 sprite of the same emotion ("if it escalates / cools")
 * Custom fields   any other value in the embedded JSON, by path
 * Template        your own layout with {{sl_*}} macros
 *
 * Uses only SillyTavern.getContext() and the standard /api/sprites route, so
 * it works as a plain third-party extension.
 */
(() => {
    'use strict';

    const MODULE = 'spritelore';
    const PROMPT_KEY = 'SPRITE_LORE';
    const META_KEYWORD = 'st_expr';
    // sprite URLs look like /characters/<folder>/<file>.png (folder may contain '/')
    const SPRITE_RE = /\/characters\/(.+)\/([^/?#]+\.png)(?:[?#].*)?$/i;

    const DEFAULTS = {
        enabled: true,
        mode: 'lore',            // lore | tags | all
        detail: 2,               // 1 lore, 2 +keywords, 3 +entries, 4 +degree/target, 5 +tags
        ladder: 'off',           // off | stronger | weaker | both
        ladderOrder: 'faint, mild, moderate, strong, intense, overwhelming',
        customFields: '',        // one "Label = path.in.json" per line
        useTemplate: false,
        template: '[{{sl_char}} feels {{sl_emotion}} ({{sl_degree}}, aimed at {{sl_target}})]\n{{sl_lore}}\nNext reply can pick up: {{sl_keywords}}',
        fallbackToTags: true,    // lore mode: use tags when a sprite has no lore yet
        reviewedOnly: false,
        position: 'in_chat',     // in_chat | in_prompt | before_prompt
        depth: 2,
        role: 'system',          // system | user | assistant
        scan: false,             // let World Info scan the injected text
        maxChars: 1500,
        disabledFolders: [],     // sprite folders this extension stays quiet for
    };

    const cache = new Map();     // url -> Promise<meta|null>
    const current = new Map();   // folder -> { url, meta, changedAt }
    let lastText = '';
    let scanTimer = null;

    const ctx = () => SillyTavern.getContext();

    function settings() {
        const all = ctx().extensionSettings;
        if (!all[MODULE]) all[MODULE] = {};
        const s = all[MODULE];
        if ('includeKeywords' in s && !('detail' in s)) {      // settings from 0.1
            s.detail = s.includeKeywords ? 2 : 1;
        }
        for (const [k, v] of Object.entries(DEFAULTS)) {
            if (!(k in s)) s[k] = Array.isArray(v) ? [...v] : v;
        }
        return s;
    }

    // ---------------------------------------------------------------- PNG

    function latin1(bytes) {
        let s = '';
        for (let i = 0; i < bytes.length; i += 0x8000) {
            s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }
        return s;
    }

    function parseMeta(buf) {
        const sig = [137, 80, 78, 71, 13, 10, 26, 10];
        for (let i = 0; i < 8; i++) if (buf[i] !== sig[i]) return null;
        const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
        let pos = 8;
        let found = null;
        while (pos + 8 <= buf.length) {
            const len = dv.getUint32(pos);
            const type = latin1(buf.subarray(pos + 4, pos + 8));
            if (type === 'tEXt') {
                const body = buf.subarray(pos + 8, pos + 8 + len);
                const z = body.indexOf(0);
                if (z > 0 && latin1(body.subarray(0, z)) === META_KEYWORD) {
                    found = latin1(body.subarray(z + 1));
                }
            }
            if (type === 'IEND') break;
            pos += 12 + len;
        }
        if (!found) return null;
        try {
            const bin = atob(found);
            const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
            return JSON.parse(new TextDecoder('utf-8').decode(bytes));
        } catch (e) {
            console.warn('[Sprite Lore] bad metadata', e);
            return null;
        }
    }

    function readMeta(url) {
        if (!cache.has(url)) {
            cache.set(url, (async () => {
                try {
                    const res = await fetch(url, { cache: 'no-cache' });
                    if (!res.ok) return null;
                    return parseMeta(new Uint8Array(await res.arrayBuffer()));
                } catch (e) {
                    console.warn('[Sprite Lore] fetch failed', url, e);
                    return null;
                }
            })());
            if (cache.size > 500) cache.delete(cache.keys().next().value);
        }
        return cache.get(url);
    }

    // ------------------------------------------------------------ sprites

    const siblingCache = new Map();   // folder -> { at, list: Promise<[{file,url}]> }

    function spriteFolderList(folder) {
        const hit = siblingCache.get(folder);
        if (hit && Date.now() - hit.at < 60000) return hit.list;
        const list = (async () => {
            try {
                const c = ctx();
                const headers = typeof c.getRequestHeaders === 'function' ? c.getRequestHeaders() : {};
                delete headers['Content-Type'];
                const res = await fetch(`/api/sprites/get?name=${encodeURIComponent(folder)}`, { headers });
                if (!res.ok) return [];
                const items = await res.json();
                return (Array.isArray(items) ? items : [])
                    .map(it => String(it.path || ''))
                    .filter(p => /\.png(?:[?#].*)?$/i.test(p))
                    .map(p => ({ file: decodeURIComponent(p.split('?')[0].split('/').pop()),
                                 url: new URL(p, location.href).href }));
            } catch (e) {
                console.warn('[Sprite Lore] sprite list failed', folder, e);
                return [];
            }
        })();
        siblingCache.set(folder, { at: Date.now(), list });
        return list;
    }

    function degreeIndex(meta, order) {
        const d = clean(meta?.lore?.intensity || meta?.plan?.intensity).toLowerCase();
        return d ? order.indexOf(d) : -1;
    }

    // lore of the next stronger / weaker sprite of the same emotion
    async function ladderFor(folder, entry, s) {
        const out = { stronger: null, weaker: null };
        if (s.ladder === 'off' || !entry.meta) return out;
        const order = String(s.ladderOrder).split(',').map(x => x.trim().toLowerCase()).filter(Boolean);
        const mine = degreeIndex(entry.meta, order);
        if (mine < 0) return out;
        const label = labelOf(entry);
        const sibs = (await spriteFolderList(folder))
            .filter(f => f.url !== entry.url && f.file.replace(/\.png$/i, '').split('-')[0].toLowerCase() === label.toLowerCase());
        for (const f of sibs) {
            const meta = await readMeta(f.url);
            if (!meta || !loreUsable(meta, s)) continue;
            const i = degreeIndex(meta, order);
            if (i < 0 || i === mine) continue;
            if (i > mine && (!out.stronger || i < out.stronger.i)) out.stronger = { i, meta };
            if (i < mine && (!out.weaker || i > out.weaker.i)) out.weaker = { i, meta };
        }
        if (s.ladder === 'stronger') out.weaker = null;
        if (s.ladder === 'weaker') out.stronger = null;
        return out;
    }

    function visible(el) {
        return el.isConnected && el.offsetParent !== null && el.style.display !== 'none'
            && !el.closest('#mes_example, .mes, #rm_print_characters_block');
    }

    async function scanSprites() {
        const found = new Map();
        for (const img of document.querySelectorAll('img')) {
            const src = img.getAttribute('src') || '';
            const m = SPRITE_RE.exec(src.split('#')[0]);
            if (!m || !visible(img)) continue;
            let folder;
            try { folder = decodeURIComponent(m[1]); } catch { folder = m[1]; }
            found.set(folder, new URL(src, location.href).href);
        }
        // drop sprites no longer on screen
        for (const folder of [...current.keys()]) {
            if (!found.has(folder)) current.delete(folder);
        }
        for (const [folder, url] of found) {
            const prev = current.get(folder);
            if (prev && prev.url === url) continue;
            const meta = await readMeta(url);
            const entry = { url, meta, changedAt: Date.now() };
            entry.ladder = await ladderFor(folder, entry, settings());
            current.set(folder, entry);
        }
        updatePrompt();
    }

    // react only to changes that can affect which sprite images are shown
    function touchesImages(node) {
        return node.nodeType === 1 && (node.tagName === 'IMG' || !!node.querySelector?.('img'));
    }

    function onMutations(records) {
        for (const r of records) {
            const t = r.target;
            if (t.nodeType === 1 && t.closest?.('.spritelore-settings, #chat')) continue;
            if (r.type === 'attributes' ? touchesImages(t)
                : [...r.addedNodes, ...r.removedNodes].some(touchesImages)) {
                scheduleScan();
                return;
            }
        }
    }

    function scheduleScan() {
        clearTimeout(scanTimer);
        scanTimer = setTimeout(() => scanSprites().catch(console.error), 150);
    }

    // ------------------------------------------------------------- format

    const clean = s => String(s ?? '').replace(/[\u0000-\u0008\u000B-\u001F\u007F]/g, '').trim();
    const list = a => (Array.isArray(a) ? a.map(clean).filter(Boolean) : []);

    function labelOf(entry) {
        const file = decodeURIComponent(entry.url.split('/').pop().split('?')[0]);
        return clean(entry.meta?.label) || file.replace(/\.png$/i, '').split('-')[0];
    }

    function tagLines(meta) {
        const vlm = meta.vlm || {};
        const cats = vlm.categories;
        const out = [];
        if (cats && typeof cats === 'object') {
            for (const k of ['appearance', 'expression', 'action', 'objects', 'setting']) {
                const v = list(cats[k]);
                if (v.length) out.push(`${k[0].toUpperCase()}${k.slice(1)}: ${v.join(', ')}`);
            }
        } else if (list(vlm.tags).length) {
            out.push(`Tags: ${list(vlm.tags).join(', ')}`);
        }
        return out;
    }

    function loreUsable(meta, s) {
        const lore = meta.lore;
        if (!lore || lore.stale || !clean(lore.text)) return false;
        if (s.reviewedOnly && !lore.reviewed) return false;
        return true;
    }

    function getPath(obj, path) {
        let cur = obj;
        for (const part of String(path).split(/[.[\]]+/).filter(Boolean)) {
            if (cur == null) return undefined;
            cur = cur[/^\d+$/.test(part) ? Number(part) : part];
        }
        return cur;
    }

    function asText(v) {
        if (v == null) return '';
        if (Array.isArray(v)) return v.map(asText).filter(Boolean).join(', ');
        if (typeof v === 'object') {
            if ('text' in v) return clean(v.text);
            return clean(JSON.stringify(v));
        }
        return clean(v);
    }

    function customFieldLines(meta, s) {
        const out = [];
        for (const line of String(s.customFields || '').split('\n')) {
            const t = line.trim();
            if (!t || t.startsWith('#')) continue;
            const eq = t.indexOf('=');
            const label = eq > 0 ? t.slice(0, eq).trim() : t;
            const path = eq > 0 ? t.slice(eq + 1).trim() : t;
            const v = asText(getPath(meta, path));
            if (v) out.push(`${label}: ${v}`);
        }
        return out;
    }

    function entryLines(meta) {
        return (Array.isArray(meta.entries) ? meta.entries : [])
            .filter(e => clean(e?.text))
            .map(e => `${clean(e.title) || 'Note'}: ${clean(e.text)}`
                + (list(e.keywords).length ? ` (${list(e.keywords).join(', ')})` : ''));
    }

    function macroValues(folder, entry, s) {
        const meta = entry.meta || {};
        const lore = meta.lore || {};
        const usable = loreUsable(meta, s);
        const vlm = meta.vlm || {};
        const ladder = entry.ladder || {};
        return {
            char: clean(meta.char) || folder.split('/')[0],
            emotion: labelOf(entry),
            lore: usable ? clean(lore.text) : '',
            keywords: usable ? list(lore.keywords).join(', ') : '',
            degree: clean(lore.intensity || meta.plan?.intensity),
            target: clean(lore.direction || meta.plan?.direction),
            tags: tagLines(meta).join('\n'),
            description: clean(vlm.description),
            entries: entryLines(meta).join('\n'),
            stronger: ladder.stronger ? clean(ladder.stronger.meta.lore.text) : '',
            weaker: ladder.weaker ? clean(ladder.weaker.meta.lore.text) : '',
        };
    }

    function fillTemplate(tpl, folder, entry, s) {
        const v = macroValues(folder, entry, s);
        const meta = entry.meta || {};
        const lines = [];
        for (const line of String(tpl).split('\n')) {
            let hadMacro = false, anyFilled = false;
            const mark = (val) => { hadMacro = true; if (clean(val)) anyFilled = true; return val; };
            const outLine = line
                .replace(/\{\{sl_field::([^}]+)\}\}/g, (_, p) => mark(asText(getPath(meta, p.trim()))))
                .replace(/\{\{sl_entry::([^}]+)\}\}/g, (_, t) => {
                    const e = (meta.entries || []).find(x => clean(x?.title).toLowerCase() === t.trim().toLowerCase());
                    return mark(e ? clean(e.text) : '');
                })
                .replace(/\{\{sl_(\w+)\}\}/g, (m, k) => (k in v ? mark(asText(v[k])) : m));
            // a line built around macros that all came out empty is dropped,
            // so "Stronger: {{sl_stronger}}" disappears when there is no stronger degree
            if (hadMacro && !anyFilled) continue;
            lines.push(outLine);
        }
        return lines.join('\n').replace(/\n{3,}/g, '\n\n').trim();
    }

    function formatEntry(folder, entry, s) {
        const meta = entry.meta;
        if (!meta || (s.disabledFolders || []).includes(folder)) return '';
        if (s.useTemplate && clean(s.template)) return fillTemplate(s.template, folder, entry, s);
        const v = macroValues(folder, entry, s);
        let mode = s.mode;
        const hasLore = !!v.lore;
        if (mode === 'lore' && !hasLore) {
            if (!s.fallbackToTags) return '';
            mode = 'tags';
        }
        const detail = Number(s.detail) || 1;
        const lines = [];
        if ((mode === 'lore' || mode === 'all') && hasLore) {
            const how = detail >= 4 && (v.degree || v.target)
                ? ` (${[v.degree, v.target && `aimed at ${v.target}`].filter(Boolean).join(', ')})` : '';
            lines.push(`[${v.char} lore, tied to feeling ${v.emotion}${how}]`, v.lore);
            if (detail >= 2 && v.keywords) lines.push(`Weave into the next reply where natural: ${v.keywords}`);
            if (detail >= 3) lines.push(...entryLines(meta));
            if (v.stronger) lines.push(`[If it escalates] ${v.stronger}`);
            if (v.weaker) lines.push(`[If it cools down] ${v.weaker}`);
        }
        const wantTags = mode === 'tags' || mode === 'all' || (detail >= 5 && hasLore);
        if (wantTags && v.tags) {
            lines.push(`[Current look of ${v.char} (${v.emotion})]`, v.tags);
            if ((mode === 'all' || detail >= 5) && v.description) lines.push(`Scene: ${v.description}`);
        }
        lines.push(...customFieldLines(meta, s));
        return lines.join('\n');
    }

    function buildText() {
        const s = settings();
        // most recently changed sprite last, so it sits closest to the reply
        const entries = [...current.entries()].sort((a, b) => a[1].changedAt - b[1].changedAt);
        let text = entries.map(([f, e]) => formatEntry(f, e, s)).filter(Boolean).join('\n\n');
        if (text.length > s.maxChars) text = text.slice(-s.maxChars).replace(/^[^\n]*\n/, '');
        const c = ctx();
        if (text && typeof c.substituteParams === 'function') {
            try { text = c.substituteParams(text); } catch { /* keep raw */ }
        }
        return text;
    }

    // ------------------------------------------------------------- inject

    function positionValue(s) {
        const t = ctx().extension_prompt_types || {};
        return { in_prompt: t.IN_PROMPT ?? 0, in_chat: t.IN_CHAT ?? 1,
                 before_prompt: t.BEFORE_PROMPT ?? 2 }[s.position] ?? (t.IN_CHAT ?? 1);
    }

    function roleValue(s) {
        const r = ctx().extension_prompt_roles || {};
        return { system: r.SYSTEM ?? 0, user: r.USER ?? 1,
                 assistant: r.ASSISTANT ?? 2 }[s.role] ?? (r.SYSTEM ?? 0);
    }

    let lastInjected = null;

    function updatePrompt() {
        const s = settings();
        lastText = buildText();
        const c = ctx();
        const inject = s.enabled && lastText ? lastText : '';
        const sig = JSON.stringify([inject, s.position, s.depth, s.scan, s.role]);
        if (typeof c.setExtensionPrompt === 'function' && sig !== lastInjected) {
            lastInjected = sig;
            c.setExtensionPrompt(PROMPT_KEY, inject, positionValue(s), Number(s.depth) || 0,
                                 !!s.scan, roleValue(s));
        }
        renderPreview();
    }

    function reset() {
        current.clear();
        updatePrompt();
        scheduleScan();
    }

    // ----------------------------------------------------------------- UI

    function renderFolders() {
        const host = document.getElementById('spritelore_folders');
        if (!host) return;
        const s = settings();
        const folders = [...current.keys()];
        host.innerHTML = '';
        if (!folders.length) {
            host.textContent = '(none)';
            return;
        }
        for (const folder of folders) {
            const label = document.createElement('label');
            label.className = 'checkbox_label';
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.checked = !(s.disabledFolders || []).includes(folder);
            box.addEventListener('change', () => {
                const set = new Set(s.disabledFolders || []);
                if (box.checked) set.delete(folder); else set.add(folder);
                s.disabledFolders = [...set];
                ctx().saveSettingsDebounced();
                updatePrompt();
            });
            label.append(box, document.createTextNode(` ${folder}`));
            host.append(label);
        }
    }

    function renderPreview() {
        const box = document.getElementById('spritelore_preview');
        const status = document.getElementById('spritelore_status');
        if (!box) return;
        renderFolders();
        box.value = lastText || '(nothing injected)';
        const total = current.size;
        const withMeta = [...current.values()].filter(e => e.meta).length;
        const withLore = [...current.values()].filter(e => e.meta?.lore?.text).length;
        let msg = `Sprites on screen: ${total} | with tags: ${withMeta} | with lore: ${withLore}`;
        if (typeof ctx().setExtensionPrompt !== 'function') {
            msg += ' | ERROR: this SillyTavern build has no setExtensionPrompt in getContext()';
        } else if (!settings().enabled) {
            msg += ' | injection off ({{spritelore}} macro still works)';
        }
        status.textContent = msg;
    }

    function settingsHtml() {
        return `
<div class="spritelore-settings">
  <div class="inline-drawer">
    <div class="inline-drawer-toggle inline-drawer-header">
      <b>Sprite Lore</b>
      <div class="inline-drawer-icon fa-solid fa-circle-chevron-down down"></div>
    </div>
    <div class="inline-drawer-content">
      <div class="spritelore-row">
        <label class="checkbox_label"><input type="checkbox" id="spritelore_enabled"> Inject into prompt</label>
      </div>
      <div class="spritelore-row">
        <label for="spritelore_mode">Send</label>
        <select id="spritelore_mode" class="text_pole">
          <option value="lore">Lore only</option>
          <option value="tags">Tags only</option>
          <option value="all">All information</option>
        </select>
      </div>
      <div class="spritelore-row">
        <label for="spritelore_detail">Lore detail</label>
        <select id="spritelore_detail" class="text_pole">
          <option value="1">1 - lore only</option>
          <option value="2">2 - + scene keywords</option>
          <option value="3">3 - + extra entries</option>
          <option value="4">4 - + degree and target</option>
          <option value="5">5 - + visual tags and description</option>
        </select>
      </div>
      <div class="spritelore-row">
        <label for="spritelore_ladder">Emotion ladder</label>
        <select id="spritelore_ladder" class="text_pole">
          <option value="off">Off</option>
          <option value="stronger">+ next stronger sprite (if it escalates)</option>
          <option value="weaker">+ next weaker sprite (if it cools down)</option>
          <option value="both">+ both</option>
        </select>
      </div>
      <div class="spritelore-row">
        <label for="spritelore_order">Degree order</label>
        <input type="text" id="spritelore_order" class="text_pole" style="flex:1" title="Mildest to strongest, as the Sprite Lore page names them">
      </div>
      <div class="spritelore-row spritelore-col">
        <label for="spritelore_fields">Custom fields <span class="spritelore-hint">one per line: Label = path in the sprite's JSON (e.g. Trigger = lore.trigger, Voice = entries.0.text)</span></label>
        <textarea id="spritelore_fields" class="text_pole spritelore-small" rows="3"></textarea>
      </div>
      <div class="spritelore-row">
        <label class="checkbox_label"><input type="checkbox" id="spritelore_usetpl"> Use my own template instead</label>
      </div>
      <div class="spritelore-row spritelore-col" id="spritelore_tplbox">
        <label for="spritelore_tpl">Template <span class="spritelore-hint">{{sl_char}} {{sl_emotion}} {{sl_lore}} {{sl_keywords}} {{sl_degree}} {{sl_target}} {{sl_tags}} {{sl_description}} {{sl_entries}} {{sl_stronger}} {{sl_weaker}} {{sl_entry::Title}} {{sl_field::path}}; ST macros work too</span></label>
        <textarea id="spritelore_tpl" class="text_pole spritelore-small" rows="5"></textarea>
      </div>
      <div class="spritelore-row">
        <label class="checkbox_label"><input type="checkbox" id="spritelore_fallback"> Lore mode: send tags when a sprite has no lore</label>
      </div>
      <div class="spritelore-row">
        <label class="checkbox_label"><input type="checkbox" id="spritelore_reviewed"> Only use lore marked reviewed</label>
      </div>
      <div class="spritelore-row">
        <label for="spritelore_position">Position</label>
        <select id="spritelore_position" class="text_pole">
          <option value="in_chat">In chat @ depth</option>
          <option value="in_prompt">After story string</option>
          <option value="before_prompt">Before story string</option>
        </select>
        <label for="spritelore_depth" style="min-width:auto">Depth</label>
        <input type="number" id="spritelore_depth" class="text_pole" min="0" max="99">
      </div>
      <div class="spritelore-row">
        <label for="spritelore_role">Role</label>
        <select id="spritelore_role" class="text_pole">
          <option value="system">System</option>
          <option value="user">User</option>
          <option value="assistant">Assistant</option>
        </select>
      </div>
      <div class="spritelore-row">
        <label class="checkbox_label"><input type="checkbox" id="spritelore_scan"> Let World Info scan the injected text</label>
      </div>
      <div class="spritelore-row">
        <label for="spritelore_max">Max characters</label>
        <input type="number" id="spritelore_max" class="text_pole" min="100" max="20000" step="100">
      </div>
      <div class="spritelore-row spritelore-col">
        <label>Sprites on screen <span class="spritelore-hint">untick to keep a character's sprites out of the prompt</span></label>
        <div id="spritelore_folders" class="spritelore-folders"></div>
      </div>
      <div class="spritelore-row">
        <span class="spritelore-status" id="spritelore_status"></span>
      </div>
      <textarea id="spritelore_preview" class="text_pole spritelore-preview" readonly></textarea>
      <div class="spritelore-row">
        <div id="spritelore_refresh" class="menu_button">Re-read sprites</div>
      </div>
    </div>
  </div>
</div>`;
    }

    function bindSettings() {
        const s = settings();
        const save = () => { ctx().saveSettingsDebounced(); updatePrompt(); };
        const bindCheck = (id, key) => {
            const el = document.getElementById(id);
            el.checked = !!s[key];
            el.addEventListener('change', () => { s[key] = el.checked; save(); });
        };
        const bindValue = (id, key, num) => {
            const el = document.getElementById(id);
            el.value = s[key];
            el.addEventListener('change', () => {
                s[key] = num ? Number(el.value) : el.value;
                save();
            });
        };
        bindCheck('spritelore_enabled', 'enabled');
        bindValue('spritelore_detail', 'detail', true);
        bindValue('spritelore_ladder', 'ladder');
        bindValue('spritelore_order', 'ladderOrder');
        bindValue('spritelore_fields', 'customFields');
        bindValue('spritelore_tpl', 'template');
        bindCheck('spritelore_usetpl', 'useTemplate');
        const tplBox = document.getElementById('spritelore_tplbox');
        const showTpl = () => { tplBox.style.display = s.useTemplate ? '' : 'none'; };
        document.getElementById('spritelore_usetpl').addEventListener('change', showTpl);
        showTpl();
        for (const id of ['spritelore_ladder', 'spritelore_order']) {
            document.getElementById(id).addEventListener('change', () => { current.clear(); scheduleScan(); });
        }
        bindCheck('spritelore_fallback', 'fallbackToTags');
        bindCheck('spritelore_reviewed', 'reviewedOnly');
        bindCheck('spritelore_scan', 'scan');
        bindValue('spritelore_mode', 'mode');
        bindValue('spritelore_position', 'position');
        bindValue('spritelore_role', 'role');
        bindValue('spritelore_depth', 'depth', true);
        bindValue('spritelore_max', 'maxChars', true);
        document.getElementById('spritelore_refresh').addEventListener('click', () => {
            cache.clear();
            siblingCache.clear();
            reset();
        });
    }

    // --------------------------------------------------------------- init

    let initialized = false;

    function init() {
        if (initialized) return;
        initialized = true;
        const c = ctx();
        const host = document.getElementById('extensions_settings2')
            || document.getElementById('extensions_settings');
        if (host && !document.getElementById('spritelore_preview')) {
            host.insertAdjacentHTML('beforeend', settingsHtml());
            bindSettings();
        }

        if (typeof c.registerMacro === 'function') {
            try { c.registerMacro('spritelore', () => lastText); } catch (e) {
                console.warn('[Sprite Lore] macro registration failed', e);
            }
        }

        new MutationObserver(onMutations).observe(document.body, {
            subtree: true, childList: true, attributes: true,
            attributeFilter: ['src', 'style', 'class'],
        });

        const ev = c.event_types || {};
        if (ev.CHAT_CHANGED) c.eventSource.on(ev.CHAT_CHANGED, reset);
        for (const name of ['MESSAGE_RECEIVED', 'CHARACTER_MESSAGE_RENDERED', 'GROUP_UPDATED']) {
            if (ev[name]) c.eventSource.on(ev[name], scheduleScan);
        }
        scheduleScan();
        console.log('[Sprite Lore] loaded');
    }

    const c0 = ctx();
    if (c0.eventSource && c0.event_types?.APP_READY) {
        c0.eventSource.once
            ? c0.eventSource.once(c0.event_types.APP_READY, init)
            : c0.eventSource.on(c0.event_types.APP_READY, init);
        // APP_READY may already have fired if the extension loads late
        if (document.getElementById('extensions_settings2')) setTimeout(init, 0);
    } else {
        jQuery(init);
    }
})();
