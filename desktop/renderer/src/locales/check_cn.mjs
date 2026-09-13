import fs from 'fs';

const locales = ['ar','zh-Hant','ja','ko','de','es','pt','tr','he'];
const zhRe = /[\u4e00-\u9fff]/;

for (const loc of locales) {
  const content = fs.readFileSync(loc + '.ts', 'utf-8');
  const lines = content.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (zhRe.test(lines[i])) {
      const strMatch = lines[i].match(/:\s*"([^"]*[\u4e00-\u9fff][^"]*)"/);
      if (strMatch && !lines[i].trim().startsWith('//') && !lines[i].trim().startsWith('*')) {
        console.log(loc + ' L' + (i+1) + ': ' + strMatch[1]);
      }
    }
  }
}
