/* ── Column editor ───────────────────────────────────────────────────────────
   Shared Settings control for choosing a table's columns: the shown columns in
   order (heading, format, decimal places, move to top/up/down, remove) on the left, and
   the table's other columns to add on the right. Styles are in theme.css
   (.col-editor). Used by the Stock List Settings popup.

   const editor = new ColumnEditor(element, {
       live:    [{column, type}],              // every column of the table
       shown:   [{column, label, format, decimals, ...}],
       formats: [[value, label]],              // choices for the format list
       numberFormats: ['number', 'gbp', 'pct'],// formats that use decimal places
       newColumn: (column, type) => ({...}),   // settings for a newly added column
   });
   editor.columns()  -> the shown list, headings trimmed. Extra keys on each
                        column (e.g. width) are kept as they came in.
─────────────────────────────────────────────────────────────────────────── */
class ColumnEditor {
    constructor(el, opts) {
        this.el = el;
        this.live = opts.live || [];
        this.shown = (opts.shown || []).map(c => ({ ...c }));
        this.formats = opts.formats || [['text', 'Text']];
        this.numberFormats = opts.numberFormats || ['number', 'gbp', 'pct'];
        this.newColumn = opts.newColumn || ((column) => ({ column, label: '', format: 'text', decimals: 0 }));

        el.innerHTML = `
            <div class="col-editor">
                <div>
                    <div class="col-editor-head" data-role="shown-head"></div>
                    <div class="col-editor-list" data-role="shown"></div>
                </div>
                <div>
                    <div class="col-editor-head">Other columns in the table</div>
                    <input type="text" class="col-editor-search" data-role="search" placeholder="Search columns">
                    <div class="col-editor-list short" data-role="hidden"></div>
                </div>
            </div>`;
        this.shownEl = el.querySelector('[data-role="shown"]');
        this.hiddenEl = el.querySelector('[data-role="hidden"]');
        this.searchEl = el.querySelector('[data-role="search"]');

        this.searchEl.addEventListener('input', () => this.renderHidden());
        el.addEventListener('input', e => {
            const i = e.target.dataset.i;
            if (e.target.dataset.act === 'label') this.shown[i].label = e.target.value;
        });
        el.addEventListener('change', e => {
            const i = e.target.dataset.i, act = e.target.dataset.act;
            if (act === 'format') { this.shown[i].format = e.target.value; this.renderShown(); }
            if (act === 'decimals') this.shown[i].decimals = parseInt(e.target.value, 10);
        });
        el.addEventListener('click', e => {
            const btn = e.target.closest('button[data-act]');
            if (!btn) return;
            const i = Number(btn.dataset.i), act = btn.dataset.act;
            if (act === 'top') {
                if (i === 0) return;
                this.shown.unshift(...this.shown.splice(i, 1));
                this.render();
            } else if (act === 'up' || act === 'down') {
                const j = i + (act === 'up' ? -1 : 1);
                if (j < 0 || j >= this.shown.length) return;
                [this.shown[i], this.shown[j]] = [this.shown[j], this.shown[i]];
                this.render();
            } else if (act === 'remove') {
                this.shown.splice(i, 1);
                this.render();
            } else if (act === 'add') {
                const col = this.live.find(c => c.column === btn.dataset.column);
                this.shown.push(this.newColumn(btn.dataset.column, col ? col.type : null));
                this.render();
            }
        });
        // The name boxes are sized by measuring them, which gives nothing while
        // their settings section or popup is hidden, so size them again once shown.
        const pane = el.closest('.settings-pane');
        if (pane) pane.addEventListener('settings-pane-open', () => this.renderShown());
        const popup = el.closest('.modal');
        if (popup) popup.addEventListener('shown.bs.modal', () => this.renderShown());
        this.render();
    }

    columns() {
        return this.shown.map(c => ({ ...c, label: (c.label || '').trim() }));
    }

    render() {
        this.renderShown();
        this.renderHidden();
    }

    renderShown() {
        const esc = ColumnEditor.esc;
        const liveNames = new Set(this.live.map(c => c.column));
        const last = this.shown.length - 1;
        this.el.querySelector('[data-role="shown-head"]').textContent = `Shown, in order (${this.shown.length})`;
        this.shownEl.innerHTML = this.shown.length ? this.shown.map((c, i) => {
            const missing = !liveNames.has(c.column);
            const hasDecimals = this.numberFormats.includes(c.format);
            const formats = this.formats.map(([value, label]) =>
                `<option value="${esc(value)}" ${value === c.format ? 'selected' : ''}>${esc(label)}</option>`).join('');
            const decimals = [0, 1, 2, 3, 4].map(n =>
                `<option value="${n}" ${n === c.decimals ? 'selected' : ''}>${n} dp</option>`).join('');
            return `
                <div class="col-editor-item${missing ? ' missing' : ''}">
                    <span class="col-editor-name" title="${esc(c.column)}${missing ? ' - not in the table' : ''}">${esc(c.column)}</span>
                    <input type="text" maxlength="60" value="${esc(c.label || '')}" placeholder="${esc(c.column)}"
                           title="Heading" data-act="label" data-i="${i}">
                    <select title="How the values show" data-act="format" data-i="${i}">${formats}</select>
                    <select title="Decimal places" data-act="decimals" data-i="${i}" ${hasDecimals ? '' : 'disabled'}>${decimals}</select>
                    <button type="button" class="btn btn-sm btn-outline-secondary" title="Move to top" data-act="top" data-i="${i}"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-angle-double-up"></i></button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" title="Move up" data-act="up" data-i="${i}"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" title="Move down" data-act="down" data-i="${i}"
                            ${i === last ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-sm btn-outline-danger" title="Remove" data-act="remove" data-i="${i}">
                        <i class="fas fa-times"></i></button>
                </div>`;
        }).join('') : '<div class="col-editor-empty">No columns chosen yet. Add them from the list on the right.</div>';

        // Name box as wide as the longest shown column name, the same on every
        // row so the heading boxes and buttons line up. Measured at its natural
        // width first, so a removed long name lets the box shrink again.
        const names = this.shownEl.querySelectorAll('.col-editor-name');
        names.forEach(n => { n.style.width = 'auto'; });
        let widest = 0;
        names.forEach(n => { widest = Math.max(widest, n.getBoundingClientRect().width); });
        names.forEach(n => { n.style.width = Math.ceil(widest) + 'px'; });
    }

    renderHidden() {
        const esc = ColumnEditor.esc;
        const shownNames = new Set(this.shown.map(c => c.column));
        const search = this.searchEl.value.trim().toUpperCase();
        this.hiddenEl.innerHTML = this.live
            .filter(c => !shownNames.has(c.column) && (!search || c.column.includes(search)))
            .map(c => `
                <div class="col-editor-item">
                    <span class="col-editor-name" title="${esc(c.column)}">${esc(c.column)}</span>
                    <span class="col-editor-type">${esc(String(c.type || '').toLowerCase())}</span>
                    <button type="button" class="btn btn-sm btn-outline-primary" data-act="add" data-column="${esc(c.column)}">
                        <i class="fas fa-plus"></i> Add</button>
                </div>`).join('') || '<div class="col-editor-empty">No other columns.</div>';
    }

    static esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, ch =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
    }
}
