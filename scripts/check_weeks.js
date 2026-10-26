// Checks that wkAgg() in index.html, run over every week, reproduces the data/*.json rows that build.py
// writes, and that a split range adds up. Run from the repo root: node scripts/check_weeks.js
const fs = require('fs');
const html = fs.readFileSync('index.html', 'utf8');
const code = html.slice(html.indexOf('/*agg*/'), html.indexOf('/*endagg*/'));
const wkAgg = new Function(code + '; return wkAgg;')();
const rd = (n) => JSON.parse(fs.readFileSync('data/' + n + '.json', 'utf8'));
const D = { games: rd('games'), gamemons: rd('gamemons'), gamemoves: rd('gamemoves'), mons: rd('mons'), schedule: rd('schedule') };
const weeks = D.schedule.map((s) => s.week).concat(D.games.map((g) => g.week));
const lo = Math.min(...weeks), hi = Math.max(...weeks);

let bad = 0, cells = 0;
const same = (a, b) => (typeof a === 'number' && typeof b === 'number' ? Math.abs(a - b) < 1e-9 : JSON.stringify(a) === JSON.stringify(b));
function compare(name, got, want, key) {
  const g = new Map(got.map((r) => [key(r), r]));
  if (got.length !== want.length) { bad++; console.log(name + ': ' + got.length + ' rows, expected ' + want.length); }
  want.forEach((w) => {
    const r = g.get(key(w));
    if (!r) { bad++; console.log(name + ': missing ' + key(w)); return; }
    Object.keys(w).forEach((f) => {
      cells++;
      if (!same(r[f], w[f])) { bad++; if (bad < 40) console.log(name + ' ' + key(w) + ' ' + f + ': got ' + JSON.stringify(r[f]) + ', expected ' + JSON.stringify(w[f])); }
    });
    if (Object.keys(r).join() !== Object.keys(w).join()) { bad++; console.log(name + ': field order/names differ', Object.keys(r).join(), Object.keys(w).join()); }
  });
}
const full = wkAgg(D, lo, hi);
compare('teams', full.teams, rd('teams'), (r) => r.player);
compare('mons', full.mons, rd('mons'), (r) => r.id);
compare('moves', full.moves, rd('moves'), (r) => r.move);
compare('teammoves', full.teammoves, rd('teammoves'), (r) => r.id);
compare('games', full.games.map((g) => g.id), D.games.map((g) => g.id), (x) => x);
console.log('full range ' + lo + '-' + hi + ': ' + cells + ' values compared, ' + bad + ' differences');

// a split range must add up for counts
const sumBy = (rows, key, f) => { const m = {}; rows.forEach((r) => { m[key(r)] = (m[key(r)] || 0) + r[f]; }); return m; };
let addBad = 0;
for (let k = lo; k < hi; k++) {
  const a = wkAgg(D, lo, k), b = wkAgg(D, k + 1, hi);
  [['teams', (r) => r.player, ['games', 'wins', 'kos', 'mons_lost', 'series_wins', 'series_losses']],
    ['mons', (r) => r.id, ['brought', 'led', 'kos', 'faints', 'turns_on_field', 'crits']],
    ['moves', (r) => r.move, ['uses', 'kos']]].forEach(([n, key, fs_]) => fs_.forEach((f) => {
    const x = sumBy(a[n], key, f), y = sumBy(b[n], key, f), z = sumBy(full[n], key, f);
    Object.keys(z).forEach((q) => { if ((x[q] || 0) + (y[q] || 0) !== z[q]) { addBad++; console.log('split ' + k + ' ' + n + ' ' + q + ' ' + f); } });
  }));
}
console.log('split ranges add up: ' + (addBad ? addBad + ' problems' : 'ok'));
process.exit(bad || addBad ? 1 : 0);
