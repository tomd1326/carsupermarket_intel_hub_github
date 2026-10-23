// Replaces the browser's own colour picker (the one where any click picks any colour) on every
// <input type="color"> on the page, including ones the page builds later. Clicking the colour box
// opens a hexagon of ready-made colours: the middle is white, each ring out is darker, and the
// hue goes round the circle. Under it is a row of greys and a box for an exact hex code.
// Picking a colour sets the input and fires the normal input/change events, so each input's own
// code runs as before.
(function () {
    const RADIUS = 8;      // rings of hexagons round the white centre
    const SIZE = 15;       // hexagon corner distance in px
    const GREYS = 11;      // white to black
    const SQRT3 = Math.sqrt(3);

    const style = document.createElement('style');
    style.textContent = `
        input[type=color] { cursor: pointer; }
        .cp-pop { position: fixed; z-index: 100000; background: var(--c-surface, #fff);
            border: 1px solid var(--c-border, #b0b0b0); border-radius: 8px; padding: 10px;
            box-shadow: 0 8px 24px rgba(0,0,0,.25); box-sizing: border-box; font-size: 0.85rem; }
        .cp-pop svg { display: block; margin: 0 auto; }
        .cp-hex { stroke: rgba(0,0,0,.18); stroke-width: 0.6; cursor: pointer; }
        .cp-hex:hover { stroke: #000; stroke-width: 2; }
        .cp-hex.cp-current { stroke: #000; stroke-width: 2.5; }
        .cp-foot { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
        .cp-box { width: 34px; height: 24px; border: 1px solid var(--c-border-strong, #6c757d); border-radius: 4px; }
        .cp-label { font-size: 0.72rem; color: var(--c-text-muted, #6c757d); line-height: 1; }
        .cp-foot input { width: 84px; padding: 2px 6px; border: 1px solid var(--c-control-border, #6c757d);
            border-radius: 4px; font-family: monospace; font-size: 0.85rem; }
        .cp-foot input.cp-bad { border-color: var(--c-text-danger, #dc3545); }
    `;
    document.head.appendChild(style);

    function hslToHex(h, s, l) {
        s /= 100; l /= 100;
        const k = n => (n + h / 30) % 12;
        const a = s * Math.min(l, 1 - l);
        const f = n => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
        return '#' + [f(0), f(8), f(4)].map(x => Math.round(x * 255).toString(16).padStart(2, '0')).join('');
    }

    function hexPoints(cx, cy, s) {
        const pts = [];
        for (let k = 0; k < 6; k++) {
            const a = (Math.PI / 180) * (60 * k + 30);
            pts.push((cx + s * Math.cos(a)).toFixed(1) + ',' + (cy + s * Math.sin(a)).toFixed(1));
        }
        return pts.join(' ');
    }

    // [{x, y, colour}] for the hexagon: white in the middle, darker with every ring, hue by angle.
    function wheelCells() {
        const cells = [];
        for (let q = -RADIUS; q <= RADIUS; q++) {
            for (let r = -RADIUS; r <= RADIUS; r++) {
                const d = Math.max(Math.abs(q), Math.abs(r), Math.abs(q + r));
                if (d > RADIUS) continue;
                const x = SIZE * SQRT3 * (q + r / 2), y = SIZE * 1.5 * r;
                let colour = '#ffffff';
                if (d > 0) {
                    const hue = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
                    colour = hslToHex(hue, 100, 92 - (d - 1) * (72 / (RADIUS - 1)));
                }
                cells.push({ x, y, colour });
            }
        }
        return cells;
    }

    function hexSvg(cells, width, height, viewX, viewY) {
        const polys = cells.map(c =>
            `<polygon class="cp-hex" points="${hexPoints(c.x, c.y, SIZE - 0.6)}" fill="${c.colour}" data-c="${c.colour}"></polygon>`
        ).join('');
        return `<svg width="${width}" height="${height}" viewBox="${viewX} ${viewY} ${width} ${height}">${polys}</svg>`;
    }

    const HALF_W = SIZE * SQRT3 * (RADIUS + 0.5) + 2;
    const HALF_H = SIZE * 1.5 * RADIUS + SIZE + 2;
    const WHEEL_SVG = hexSvg(wheelCells(), Math.round(HALF_W * 2), Math.round(HALF_H * 2), -HALF_W, -HALF_H);
    const GREY_CELLS = Array.from({ length: GREYS }, (_, i) => {
        const v = Math.round(255 - (255 * i) / (GREYS - 1));
        return { x: i * SIZE * SQRT3, y: 0, colour: '#' + v.toString(16).padStart(2, '0').repeat(3) };
    });
    const GREY_W = Math.round((GREYS - 1) * SIZE * SQRT3 + SIZE * SQRT3 + 4);
    const GREY_SVG = hexSvg(GREY_CELLS, GREY_W, Math.round(SIZE * 2 + 4), -SIZE * SQRT3 / 2 - 2, -SIZE - 2);

    let pop = null;

    function closePop() {
        if (!pop) return;
        pop.el.remove();
        document.removeEventListener('mousedown', pop.outside, true);
        document.removeEventListener('keydown', pop.keys, true);
        window.removeEventListener('resize', closePop);
        const input = pop.input;
        pop = null;
        input.focus();
    }

    function choose(input, colour) {
        input.value = colour;
        closePop();
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function open(input) {
        if (pop) closePop();
        const el = document.createElement('div');
        el.className = 'cp-pop';
        el.innerHTML = WHEEL_SVG + '<div style="height:6px"></div>' + GREY_SVG + `
            <div class="cp-foot">
                <div><div class="cp-label">Current</div><div class="cp-box" id="cp-cur"></div></div>
                <div><div class="cp-label">Hover</div><div class="cp-box" id="cp-hov"></div></div>
                <div><div class="cp-label">Hex Code</div><input type="text" id="cp-hex" maxlength="7" spellcheck="false"></div>
            </div>`;
        // Inside a Bootstrap popup the picker must live in it, or the popup takes the focus back
        // from the hex code box.
        (input.closest('.modal') || document.body).appendChild(el);

        const current = (input.value || '#000000').toLowerCase();
        const curBox = el.querySelector('#cp-cur'), hovBox = el.querySelector('#cp-hov'), hexBox = el.querySelector('#cp-hex');
        curBox.style.background = current;
        hovBox.style.background = current;
        hexBox.value = current;
        el.querySelectorAll('.cp-hex').forEach(h => { if (h.dataset.c === current) h.classList.add('cp-current'); });

        el.addEventListener('mouseover', e => {
            const c = e.target.dataset && e.target.dataset.c;
            if (c) { hovBox.style.background = c; hexBox.value = c; }
        });
        el.addEventListener('mouseleave', () => { hovBox.style.background = current; hexBox.value = current; });
        el.addEventListener('click', e => {
            const c = e.target.dataset && e.target.dataset.c;
            if (c) choose(input, c);
        });
        hexBox.addEventListener('input', () => {
            let v = hexBox.value.trim();
            if (v && v[0] !== '#') v = '#' + v;
            const ok = /^#[0-9a-fA-F]{6}$/.test(v);
            hexBox.classList.toggle('cp-bad', !ok && hexBox.value.length >= 6);
            if (ok) hovBox.style.background = v;
        });
        hexBox.addEventListener('keydown', e => {
            if (e.key !== 'Enter') return;
            let v = hexBox.value.trim();
            if (v && v[0] !== '#') v = '#' + v;
            if (/^#[0-9a-fA-F]{6}$/.test(v)) choose(input, v.toLowerCase());
            else hexBox.classList.add('cp-bad');
        });

        const rect = input.getBoundingClientRect();
        const width = el.offsetWidth, height = el.offsetHeight;
        const below = window.innerHeight - rect.bottom - 8 >= height || rect.top < height;
        el.style.left = Math.max(4, Math.min(rect.left, window.innerWidth - width - 4)) + 'px';
        el.style.top = Math.max(4, below ? Math.min(rect.bottom + 4, window.innerHeight - height - 4) : rect.top - height - 4) + 'px';

        const outside = e => { if (!el.contains(e.target)) { e.preventDefault(); closePop(); } };
        const keys = e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePop(); } };
        document.addEventListener('mousedown', outside, true);
        document.addEventListener('keydown', keys, true);
        window.addEventListener('resize', closePop);
        pop = { el, input, outside, keys };
    }

    function colourInput(e) {
        const t = e.target;
        return t && t.tagName === 'INPUT' && t.type === 'color' && !t.disabled ? t : null;
    }

    // Capture phase, so this runs before the browser opens its own picker.
    document.addEventListener('mousedown', e => {
        const input = colourInput(e);
        if (!input || e.button !== 0) return;
        e.preventDefault();
        open(input);
    }, true);
    document.addEventListener('click', e => { if (colourInput(e)) e.preventDefault(); }, true);
    document.addEventListener('keydown', e => {
        const input = colourInput(e);
        if (!input || pop) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(input); }
    }, true);
})();
