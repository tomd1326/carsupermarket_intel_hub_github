/* Start date -> end date.
 *
 * After a start date is chosen, the matching end date box takes focus and its
 * calendar opens, so nobody has to click again. Add a pair to PAIRS to cover a
 * new From / To (or Start / End) pair of date boxes.
 */
(function () {
    'use strict';

    const PAIRS = {
        'sa-b-from': 'sa-b-to',      // Guided Self-Analytics
        'date-from': 'date-to',      // Trade Report, Reports
        'expFrom': 'expTo',          // Cars Bought export
        'mDateFrom': 'mDateTo',      // Cars Bought
        'allFrom': 'allTo',          // Cars Bought table
        'startInput': 'endInput',    // DSE
        'f-from': 'f-to',            // Logs, User Activity
    };

    document.addEventListener('change', e => {
        const start = e.target;
        if (!start || start.type !== 'date' || !PAIRS[start.id] || !start.value) return;
        // While a year is being typed the box passes through years like 0002 and
        // 0202; only move on once the date looks complete.
        const year = Number(start.value.slice(0, 4));
        if (year < 1900) return;
        const end = document.getElementById(PAIRS[start.id]);
        if (!end || end.disabled) return;
        end.focus();
        if (typeof end.showPicker === 'function') {
            try { end.showPicker(); } catch (err) { /* needs a recent click; focus alone is fine */ }
        }
    });
})();
