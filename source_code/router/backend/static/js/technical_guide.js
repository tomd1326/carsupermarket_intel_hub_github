// Technical Guide: builds the contents list down the left from the guide's own
// h2 (section) and h3 (sub-section) headings, so there is only one list to keep
// up to date. Highlights the section being read and filters by a search word.
(function () {
    const text = document.getElementById('tg-text');
    const toc = document.getElementById('tg-toc');
    const filter = document.getElementById('tg-filter');
    if (!text || !toc) return;

    const slug = s => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const links = [];
    const used = new Set();
    text.querySelectorAll('h2, h3').forEach(h => {
        if (!h.id) {
            let id = slug(h.textContent) || 'section';
            while (used.has(id)) id += '-x';
            h.id = id;
        }
        used.add(h.id);
        const a = document.createElement('a');
        a.href = '#' + h.id;
        a.className = h.tagName === 'H2' ? 'tg-toc-h2' : 'tg-toc-h3';
        a.textContent = h.textContent;
        toc.appendChild(a);
        links.push({ a, h, section: h.closest('section') });
    });

    // The heading nearest above the top of the window is the one being read.
    let ticking = false;
    function markCurrent() {
        ticking = false;
        let current = null;
        for (const l of links) {
            if (l.h.offsetParent === null) continue;
            if (l.h.getBoundingClientRect().top <= 80) current = l;
            else break;
        }
        links.forEach(l => l.a.classList.toggle('active', l === current));
    }
    window.addEventListener('scroll', () => {
        if (!ticking) { ticking = true; requestAnimationFrame(markCurrent); }
    }, { passive: true });
    markCurrent();

    // Shows only the sections that contain the word, and their contents links.
    if (filter) {
        filter.addEventListener('input', () => {
            const word = filter.value.trim().toLowerCase();
            const sections = text.querySelectorAll(':scope > section');
            sections.forEach(s => {
                const hit = !word || s.textContent.toLowerCase().includes(word);
                s.classList.toggle('tg-hidden', !hit);
            });
            links.forEach(l => {
                const hidden = l.section && l.section.classList.contains('tg-hidden');
                const subHit = !word || l.h.tagName === 'H2' || (l.h.nextElementSibling && sectionText(l.h).includes(word));
                l.a.classList.toggle('tg-hidden', hidden || !subHit);
            });
            markCurrent();
        });
    }

    // The text from a heading down to the next heading of the same or higher level.
    function sectionText(h) {
        let out = h.textContent;
        let el = h.nextElementSibling;
        while (el && !/^H[23]$/.test(el.tagName)) { out += ' ' + el.textContent; el = el.nextElementSibling; }
        return out.toLowerCase();
    }
})();
