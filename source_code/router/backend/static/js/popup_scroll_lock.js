/* Stops the page behind a popup from scrolling while the popup is open, so only
   the popup itself scrolls. Loaded on every page through the account_nav macro in
   templates/_navigation.html.

   It watches the page rather than hooking each popup's open and close code, because
   the popups are opened in different ways (an "open" class, display toggled in
   JS, elements added and removed). A popup counts as open when one of the
   backdrops below is actually shown. Bootstrap modals are not listed: Bootstrap
   already locks the page for those.

   A new popup is covered by adding data-scroll-lock to its backdrop element, or
   by adding its class or id to POPUP_SELECTOR. */
(function () {
    // A page that calls account_nav twice loads this twice.
    if (window.__popupScrollLockLoaded) return;
    window.__popupScrollLockLoaded = true;

    var POPUP_SELECTOR = [
        '.modal-overlay',           // KPI and Google Ads popups, shown with the "open" class
        '.vehicle-modal-backdrop',  // Buying Requirements popups (vehicle list, columns, log)
        '#vehicle-modal-backdrop',  // Orders & Deliveries vehicle list, added and removed by JS
        '.spec-modal',              // Assess Vehicle spec picker
        '.img-lightbox',            // Assess Vehicle full-size image
        '[data-scroll-lock]'
    ].join(',');

    var root = document.documentElement;
    var locked = false;
    var savedOverflow = '';
    var savedPaddingRight = '';
    var queued = false;

    function popupIsOpen() {
        var els = document.querySelectorAll(POPUP_SELECTOR);
        for (var i = 0; i < els.length; i++) {
            // No client rects means display:none or detached; fixed elements
            // have no offsetParent, so offsetParent cannot be used here.
            if (els[i].getClientRects().length > 0) return true;
        }
        return false;
    }

    function update() {
        queued = false;
        var open = popupIsOpen();
        if (open === locked) return;
        locked = open;
        if (open) {
            // Measured before the scrollbar goes, so the page behind does not
            // jump sideways by the scrollbar's width.
            var scrollbarWidth = window.innerWidth - root.clientWidth;
            savedOverflow = root.style.overflow;
            savedPaddingRight = root.style.paddingRight;
            root.style.overflow = 'hidden';
            if (scrollbarWidth > 0) root.style.paddingRight = scrollbarWidth + 'px';
        } else {
            root.style.overflow = savedOverflow;
            root.style.paddingRight = savedPaddingRight;
        }
    }

    function queueUpdate() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(update);
    }

    new MutationObserver(queueUpdate).observe(root, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: ['class', 'style', 'hidden']
    });
    queueUpdate();
})();
