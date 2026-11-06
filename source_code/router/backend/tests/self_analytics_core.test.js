const test = require('node:test');
const assert = require('node:assert/strict');
const C = require('../static/js/self_analytics_core.js');
const columns = ['date', 'buyer', 'actual', 'channel'].map(name => ({name}));
const rows = [
    ['2026-01-01', 10, 20, 'A'], ['2026-01-02', null, 80, 'A'],
    ['2026-01-03', 30, 60, 'B'], ['2026-02-01', 0, 0, 'A']
];
const chart = {type: 'bar', x: 'date', grain: 'month', cohort: 'complete', split: null,
    filters: [], measures: [{column: 'buyer', agg: 'avg'}, {column: 'actual', agg: 'avg'}], sort: 'x', top_n: 0};
test('matched means use the same vehicle population and preserve zero', () => {
    const d = C.buildSeries(chart, columns, rows);
    assert.deepEqual(d.series.map(s => s.data), [[20, 0], [40, 0]]);
    assert.deepEqual(d.series[0].counts, [2, 1]);
    assert.equal(d.rows, 3);
});
test('available means expose different denominators', () => {
    const d = C.buildSeries({...chart, cohort: 'available'}, columns, rows);
    assert.deepEqual(d.series.map(s => s.counts), [[2, 1], [3, 1]]);
    assert.equal(d.series[1].data[0], 160 / 3);
});
test('supplier filtering recomputes from rows, without averaging subgroup averages', () => {
    const c = {...chart, filters: [{column: 'channel', op: '=', value: 'A'}]};
    assert.equal(C.buildSeries(c, columns, rows).series[1].data[0], 20);
});
test('ratio uses sums and leaves zero denominators blank', () => {
    const c = {...chart, measures: [{column: 'actual', denominator: 'buyer', agg: 'sum'}]};
    assert.deepEqual(C.buildSeries(c, columns, rows).series[0].data, [200, null]);
});
test('drill-down uses the same filters, matching, grouping and split', () => {
    const c = {...chart, split: 'channel'};
    const d = C.buildSeries(c, columns, rows);
    assert.deepEqual(C.drillRows(c, columns, rows, d.keys[0], 'A'), [rows[0]]);
});
test('missing filter columns fail closed', () => {
    const c = {...chart, filters: [{column: 'missing', op: '=', value: 'A'}]};
    const d = C.buildSeries(c, columns, rows);
    assert.equal(d.rows, 0);
    assert.ok(d.notes[0].includes('missing'));
});
test('large results do not overflow the stack for min/max', () => {
    assert.equal(C.aggregate(Array(200000).fill(2), 'max'), 2);
});
test('invalid numeric values cannot break the matched population', () => {
    const extra = [...rows, ['2026-01-04', 'N/A', 1000, 'A'], ['2026-01-05', Infinity, 2000, 'A']];
    assert.deepEqual(C.buildSeries(chart, columns, extra).series.map(s => s.data), [[20, 0], [40, 0]]);
});
