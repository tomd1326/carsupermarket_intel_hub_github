// Adds a search box to every long dropdown (<select>) on the page, including ones the page
// builds later. A dropdown with MIN_OPTIONS or more options opens a small search popup instead
// of the browser's own list. Picking an item sets the select and fires the normal
// input/change events, so each select's own onchange code runs as before.
(function () {
    const MIN_OPTIONS = 8;   // shorter dropdowns keep the normal browser list

    const style = document.createElement('style');
    style.textContent = `
        .ss-pop { position: fixed; z-index: 100000; background: var(--c-surface, #fff);
            border: 1px solid var(--c-border, #b0b0b0); border-radius: 6px;
            box-shadow: 0 6px 18px rgba(0,0,0,.2); display: flex; flex-direction: column;
            box-sizing: border-box; font-size: 0.9rem; }
        .ss-pop input { margin: 6px; padding: 5px 8px; border: 1px solid var(--c-control-border, #6c757d);
            border-radius: 4px; font: inherit; box-sizing: border-box; width: calc(100% - 12px); }
        .ss-list { overflow-y: auto; flex: 1 1 auto; padding-bottom: 4px; }
        .ss-group { padding: 4px 10px 2px; font-weight: 600; color: var(--c-text-heading, #1e3a5f); }
        .ss-item { padding: 4px 10px; cursor: pointer; color: var(--c-text, #343a40); }
        .ss-item.ss-grouped { padding-left: 20px; }
        .ss-item.ss-active { background: var(--c-table-header-bg, #dbeafe); }
        .ss-item.ss-current { font-weight: 600; }
        .ss-empty { padding: 6px 10px; color: var(--c-text-muted, #6c757d); }
    `;
    document.head.appendChild(style);

    let pop = null;

    function closePop() {
        if (!pop) return;
        const sel = pop.select;
        pop.el.remove();
        document.removeEventListener('mousedown', pop.outside, true);
        window.removeEventListener('resize', closePop);
        pop = null;
        sel.focus();
    }

    function isLong(sel) {
        return !sel.multiple && !sel.disabled && sel.options.length >= MIN_OPTIONS;
    }

    function open(sel) {
        if (pop) closePop();
        const items = [];
        let lastGroup = null;
        Array.from(sel.children).forEach(child => {
            if (child.tagName === 'OPTGROUP') {
                Array.from(child.children).forEach((o, i) => items.push({ o, group: child.label, first: i === 0 }));
            } else if (child.tagName === 'OPTION') {
                items.push({ o: child, group: null });
            }
        });

        const el = document.createElement('div');
        el.className = 'ss-pop';
        const input = document.createElement('input');
        input.type = 'text';
        input.placeholder = 'Search...';
        const list = document.createElement('div');
        list.className = 'ss-list';
        el.appendChild(input);
        el.appendChild(list);
        // Inside an open popup, or the popup takes the cursor back from the search box.
        (sel.closest('.modal') || document.body).appendChild(el);

        const rect = sel.getBoundingClientRect();
        const width = Math.max(rect.width, 260);
        const spaceBelow = window.innerHeight - rect.bottom - 12;
        const spaceAbove = rect.top - 12;
        const below = spaceBelow >= 220 || spaceBelow >= spaceAbove;
        el.style.width = width + 'px';
        el.style.left = Math.max(4, Math.min(rect.left, window.innerWidth - width - 4)) + 'px';
        el.style.maxHeight = Math.min(420, below ? spaceBelow : spaceAbove) + 'px';
        if (below) el.style.top = rect.bottom + 2 + 'px';
        else el.style.bottom = window.innerHeight - rect.top + 2 + 'px';

        let shown = [];
        let active = 0;

        function setActive(n, scroll) {
            const rows = list.querySelectorAll('.ss-item');
            if (!rows.length) return;
            active = (n + rows.length) % rows.length;
            rows.forEach((r, i) => r.classList.toggle('ss-active', i === active));
            if (scroll) rows[active].scrollIntoView({ block: 'nearest' });
        }

        function pick(item) {
            sel.value = item.o.value;
            closePop();
            sel.dispatchEvent(new Event('input', { bubbles: true }));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }

        function render() {
            const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
            shown = items.filter(it => {
                if (it.o.disabled) return false;
                const text = ((it.group || '') + ' ' + it.o.textContent).toLowerCase();
                return words.every(w => text.includes(w));
            });
            list.innerHTML = '';
            if (!shown.length) {
                list.innerHTML = '<div class="ss-empty">Nothing found</div>';
                return;
            }
            lastGroup = null;
            shown.forEach((it, i) => {
                if (it.group !== null && it.group !== lastGroup) {
                    const g = document.createElement('div');
                    g.className = 'ss-group';
                    g.textContent = it.group;
                    list.appendChild(g);
                }
                lastGroup = it.group;
                const row = document.createElement('div');
                row.className = 'ss-item' + (it.group !== null ? ' ss-grouped' : '')
                    + (it.o.value === sel.value ? ' ss-current' : '');
                row.textContent = it.o.textContent;
                row.addEventListener('mousedown', e => { e.preventDefault(); pick(it); });
                row.addEventListener('mousemove', () => setActive(i, false));
                list.appendChild(row);
            });
            const cur = shown.findIndex(it => it.o.value === sel.value);
            setActive(input.value ? 0 : Math.max(cur, 0), true);
        }

        input.addEventListener('input', render);
        input.addEventListener('keydown', e => {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePop(); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1, true); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1, true); }
            else if (e.key === 'Enter') { e.preventDefault(); if (shown[active]) pick(shown[active]); }
        });

        const outside = e => { if (!el.contains(e.target)) { e.preventDefault(); closePop(); } };
        document.addEventListener('mousedown', outside, true);
        window.addEventListener('resize', closePop);
        pop = { el, select: sel, outside };
        render();
        input.focus();
    }

    // Capture phase, so this runs before the browser opens its own list.
    document.addEventListener('mousedown', e => {
        const sel = e.target.closest && e.target.closest('select');
        if (!sel || !isLong(sel) || e.button !== 0) return;
        e.preventDefault();
        open(sel);
    }, true);

    document.addEventListener('keydown', e => {
        const sel = e.target;
        if (!sel || sel.tagName !== 'SELECT' || !isLong(sel) || pop) return;
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            open(sel);
        }
    }, true);
})();
