import fs from 'fs';

const zh = fs.readFileSync('zh.ts', 'utf-8').split('\n');
console.log('=== English string values in zh.ts ===');
for (let i = 0; i < zh.length; i++) {
  const m = zh[i].match(/:\s*"([a-zA-Z][^"]*)"/);
  if (m) {
    const val = m[1];
    if (/^[a-zA-Z\s\-:/.()]+$/.test(val) && !/[\u4e00-\u9fff]/.test(val)) {
      console.log('zh L' + (i+1) + ': ' + zh[i].trim());
    }
  }
}
