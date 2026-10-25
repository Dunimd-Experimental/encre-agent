visual elements, no known IP
- Default provides four basic interfaces: start, restart, score display, end state
- Default includes basic error fallback and high score save

### Recommended File Structure

**Single file:**
```
index.html
```

**Multi-file:**
```
/
├── index.html
├── js/
│   └── main.js
├── images/
│   ├── bg.png
│   └── icon.png
└── audio/
    └── bgm.mp3
```

### Reference Error Fallback

```javascript
window.addEventListener('load', () => {
  try {
    initApp();
  } catch (error) {
    console.error(error);
    const fallback = document.createElement('div');
    fallback.style.cssText = 'position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:#fff;color:#111;font-size:18px;padding:24px;text-align:center;z-index:9999;';
    fallback.textContent = 'Oops, something went wrong. Please restart.';
    document.body.innerHTML = '';
    document.body.appendChild(fallback);
  }
});
```

### Gameplay Appendix

Reference content for quickly completing gameplay rules.

#### Match-3
- Recommended 8x8 grid
- Recommended 5-6 element types
- Must include swap, match, drop, refill, deadlock handling
- Suggest adding move limit and target score

#### Merge
- Define level sequence clearly
- Require collision detection and merge feedback
- Must have end line or overflow check

#### Runner
- Default landscape is more suitable
- At minimum include: jump, obstacles, scoring, restart
- Speed increase should be smooth

#### Board Games & Puzzles
- Must ensure solvability
- Must have error prompts, restart, or new game mechanism
- Portrait preferred

#### Interactive Story
- Branch data should be embedded directly in code
- At least 3 endings for completeness
- Must NOT use fetch to load external story JSON

#### Physics Simulation
- Only implement necessary lightweight physics
- Watch performance overhead
- Default landscape is more suitable

### Common Failure Reasons

| Problem | Common Cause | Solution |
|---------|-------------|----------|
| Missing entry after upload | `index.html` not at root | Ensure archive root directly contains `index.html` |
| White screen | Runtime error or wrong path | Add global fallback and check relative paths |
| Broken resources | External links or absolute paths | Change all to local relative paths |
| Package size exceeded | Images, audio, libraries too large | Compress resources and reduce unnecessary assets |
| Display issues on phone | No responsive layout or fixed pixel layout | Use responsive layout or Canvas scaling |
