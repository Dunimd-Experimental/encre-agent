import fs from 'fs';

// Step 1: Extract all English string values from zh.ts
const zhLines = fs.readFileSync('zh.ts', 'utf-8').split('\n');
const enValues = {};
for (const line of zhLines) {
  const m = line.match(/^\s*(\w+):\s*"([a-zA-Z][^"]*)"/);
  if (m) {
    const key = m[1];
    const val = m[2];
    // Only pure English values (no Chinese, no spaces that suggest natural language)
    if (/^[a-zA-Z\s\-:/.()]+$/.test(val) && !/[一-龥]/.test(val)) {
      enValues[key] = val;
    }
  }
}

console.log('Found', Object.keys(enValues).length, 'English keys in zh.ts');

// Step 2: For each locale, fix keys that should stay English
const locales = ['ar','zh-Hant','ja','ko','de','es','pt','tr','he'];
let totalFixes = 0;

for (const loc of locales) {
  let content = fs.readFileSync(loc + '.ts', 'utf-8');
  let fixes = 0;
  
  for (const [key, enVal] of Object.entries(enValues)) {
    // Find the line: key: "..." in the locale file
    const re = new RegExp('^(\\s*' + key + ':\\s*)"[^"]*"(,?)', 'm');
    const match = content.match(re);
    if (match) {
      const currentVal = match[0].match(/"([^"]*)"/);
      if (currentVal && currentVal[1] !== enVal) {
        // Replace the value with the English value from zh.ts
        content = content.replace(re, '$1"' + enVal + '"$2');
        fixes++;
      }
    }
  }
  
  if (fixes > 0) {
    fs.writeFileSync(loc + '.ts', content, 'utf-8');
    console.log(loc + ': fixed ' + fixes + ' keys');
    totalFixes += fixes;
  }
}

console.log('Total fixes:', totalFixes);
