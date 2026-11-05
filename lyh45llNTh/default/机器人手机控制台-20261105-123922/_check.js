// 临时校验脚本：检查页面内联 JS 语法与元素 ID 完整性
const fs = require('fs');
const p = process.argv[2];
const s = fs.readFileSync(p, 'utf8');

// 提取内联脚本（跳过带 src 的 CDN 引用）
const re = /<script>([\s\S]*?)<\/script>/g;
let m, idx = 0, bad = 0;
while ((m = re.exec(s)) !== null) {
  idx++;
  try {
    new Function(m[1]);
    console.log('script #' + idx + ' OK, ' + m[1].length + ' bytes');
  } catch (e) {
    bad++;
    console.log('script #' + idx + ' SYNTAX ERROR: ' + e.message);
  }
}
if (idx === 0) console.log('NO INLINE SCRIPT FOUND');

// 收集 $('id') 引用，检查页面中是否都存在对应 id
const ids = [...s.matchAll(/\$\('([^']+)'\)/g)].map(x => x[1]);
const uniq = [...new Set(ids)];
const missing = uniq.filter(i => !s.includes('id="' + i + '"'));
console.log('referenced ids: ' + uniq.length);
console.log('missing ids: ' + (missing.length ? missing.join(', ') : 'none'));

// 基础结构检查
const checks = {
  'viewport meta': s.includes('name="viewport"'),
  'box-sizing': s.includes('box-sizing: border-box'),
  'no 100vw': !s.includes('width: 100vw'),
  'lang zh-CN': s.includes('lang="zh-CN"')
};
Object.keys(checks).forEach(k => console.log((checks[k] ? 'PASS ' : 'FAIL ') + k));

console.log('html total bytes = ' + s.length);
process.exit(bad || missing.length ? 1 : 0);
