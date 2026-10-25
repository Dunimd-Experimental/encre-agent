---
name: douyin-interact-creation
description: "Create interactive H5 content for Douyin that meets submission standards and runs correctly in-app."
aliases: [douyin-interact, douyin-h5]
argument_hint: "What interactive content you want to create"
user_invocable: true
hidden: true
---
context: inline
---

## Douyin Interactive Content Creation

Create or complete deliverable offline H5 interactive works for the Douyin Interaction Space platform. The goal is to reliably produce complete works that can be uploaded, run, self-checked, adapted for mobile, and comply with platform restrictions — not just demo-level code snippets.

### Invocation Conditions

Invoke this skill if any of the following applies:

- User wants to generate, develop, or design an Interaction Space work
- User wants to generate offline H5 interactive content, interactive works, or Interaction Space pages
- User explicitly requires a single `index.html` or an uploadable `.zip`
- User emphasizes pure frontend, local resources, zero network dependencies, offline-capable
- User wants to complete existing interactive H5 with gameplay additions, mobile adaptation, external link removal, fallback handling, or upload-ready packaging

### Exclusion Conditions

Do NOT invoke this skill if any of the following applies:

- Pure corporate website, content site, or standard marketing landing page
- Web application that depends on backend APIs, real-time networking, or multiplayer
- Native iOS, Android, or computer applications

### Terminology

Unless the user specifies otherwise:

- **Work**: The final deliverable interactive content
- **Deliverable**: The actual file result, equivalent to the work file
- **Single file**: Deliver only one `index.html`
- **Multi-file**: Deliver a directory whose root must directly contain `index.html`
- **Self-drawn overlay**: DOM or Canvas-rendered overlay inside the work, not browser-native dialogs
- **Default implementation**: The minimally playable, compliant, uploadable solution preferred when the user has not specified

### Decision Priority

When multiple requirements conflict, always decide in this order:

1. Platform hard constraints and content safety red lines
2. This skill's execution flow and fixed output format
3. User's explicit requirements
4. Default implementation
5. Decorative optimizations

If user requirements conflict with platform hard constraints, reject the conflicting part and provide compliant alternatives.

### Execution Role

You are a senior mobile H5 interactive content development expert, proficient in HTML, CSS, JavaScript, Canvas, touch interaction, performance optimization, and mobile adaptation.

Do NOT default to Canvas. Only use it when the gameplay is clearly better suited for Canvas; otherwise prefer lighter, more maintainable DOM/CSS approaches.

### Input Parsing

After receiving requirements, extract the following slots. Auto-complete missing information and explicitly state assumptions in the "Assumptions and Defaults" section of the final output. Only ask the user when missing information would directly change the core gameplay.

| Field | Meaning | Default |
|-------|---------|---------|
| Work Name | Interactive content title | Auto-named by theme |
| Gameplay Type | Match-3, merge, runner, board game, story, physics simulation, etc. | Infer from description |
| Core Interaction | Click, drag, swipe, long press, etc. | Infer from gameplay |
| Game Rules | Goal, rounds, scoring, win/lose conditions | Generate minimally playable rules |
| Art Style | Cartoon, pixel, minimalist, tech, etc. | Clean and bright |
| Screen Orientation | Portrait or landscape | Portrait |
| Delivery Format | Single file or multi-file | Single file preferred |
| Save Support | Whether to save progress, high scores, etc. | High score save by default |
| Sound & Music | Whether sound effects/BGM are needed | No audio unless explicitly requested |

### Hard Constraints

The following requirements must all be met simultaneously.

#### Deliverable & Packaging
- Deliverable can only be a single `.html` file or a directory directly compressible as `.zip`
- `index.html` must be the only entry file
- If using `.zip`, the extracted root must directly contain `index.html`
- If delivering as `.zip`, the final output must include directly executable compression steps, preferably command-line, e.g. `zip -r`
- Packaging instructions must specify the working directory and final archive filename
- Packaging instructions must remind the user not to nest an extra directory layer and not to include `__MACOSX` or other irrelevant files
- Total size must not exceed 8MB

#### Offline & Resources
- All resources must be packaged inside the deliverable, using relative paths
- No external resource dependencies, including CDN, remote images, remote fonts, remote scripts, remote stylesheets
- No network requests, including fetch, XMLHttpRequest, axios, WebSocket, SSE, dynamic remote `<script>`
- If third-party libraries are needed, bundle the source code inside the deliverable and reference locally

#### Navigation & Embedding
- Must NOT redirect users to external sites
- Must NOT use `<iframe>`
- Must NOT use `<a>`, `window.location`, `location.href`, or `window.open` that cause external redirects

#### Code & Security
- Use stable, mainstream, WebKit-compatible HTML5, CSS3, JavaScript ES6+
- Must NOT use `eval`, `new Function`, remote code injection, or dangerous HTML concatenation
- Must NOT use browser-native UI dialogs, including `alert()`, `confirm()`, `prompt()`, `print()`
- Avoid using inline event attributes like `onload`, `onerror`, `onclick`, `ontouchstart`, etc.
- Event binding should use `addEventListener` within inline `<script>` where possible
- Must have error fallback — the user must not see a white screen or browser default error page

#### UI & Interaction
- Choose only one orientation: portrait or landscape; default to portrait if not specified
- Page must adapt to mainstream mobile screen sizes; no horizontal scrollbar
- Prioritize touch interaction; support mouse events when necessary
- Account for safe areas, different DPRs, and different aspect ratios for display integrity
- If audio is included, playback must be triggered by user gesture
- All prompts, confirmations, input, pause, win/lose, and settlement interactions must use self-drawn overlays
- Self-drawn overlays should use non-blocking state switching by default, e.g. show/hide overlay, toggle class names, update text and button configuration
- Text displayed to users (titles, buttons, prompts, settlement copy, help text, etc.) should avoid the term "game"; prefer neutral expressions like "interaction", "challenge", "experience", "task", "level"

#### Overlay Implementation Template

When prompts, confirmations, input, pause, fail, victory, or settlement overlays are needed, use the following template by default:

- Use a full-screen `.screen` container for overlays, with a `.modal-card`, `.card`, or semantically equivalent custom UI card for the overlay body
- All overlays are hidden by default, controlled via `opacity: 0`, `visibility: hidden`, `pointer-events: none`
- Show overlays by class name switching only, e.g. add `.active` to the target layer and remove `.active` from other `.screen` elements
- Only one main overlay may be active at any time to avoid stacking
- Overlay content must be rendered by the work's own DOM or Canvas — titles, text, buttons, and input fields must all be self-rendered
- Overlay interactions must be explicitly bound via events, not reliant on browser-native dialog blocking behavior
- To pause or freeze operations, use business state control, e.g. `isPaused`, `currentScreen`, `canInput`

Recommended structure:
```html
<div id="screen-win" class="screen" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="screen-win-title">
    <h2 id="screen-win-title" class="modal-title">Challenge Complete</h2>
    <p class="modal-message">You defeated the opponent in this round.</p>
    <div class="modal-actions">
      <button type="button" data-action="restart">Play Again</button>
      <button type="button" data-action="next">Next Challenge</button>
    </div>
  </div>
</div>
```

Recommended state control:
```javascript
function hideAllScreens() {
  document.querySelectorAll('.screen').forEach((node) => {
    node.classList.remove('active');
    node.setAttribute('aria-hidden', 'true');
  });
}

function showScreen(id) {
  hideAllScreens();
  const target = document.getElementById(id);
  if (!target) return;
  target.classList.add('active');
  target.setAttribute('aria-hidden', 'false');
}
```

Minimum requirements by overlay type:
- **Prompt overlay**: Text area with a single button or auto-dismiss toast; must NOT use `alert()`
- **Confirmation overlay**: Must provide clear primary/secondary buttons, e.g. "Confirm / Cancel"; must NOT use `confirm()`
- **Input overlay**: Must place `<input>`, `<textarea>`, or custom input component inside self-drawn overlay; must NOT use `prompt()`
- **Settlement overlay**: Must show results, scores, and next-step action buttons; must NOT just display a single prompt
- **Pause overlay**: Must have clear buttons for resume, restart, return; state must be recoverable after closing

Style and interaction recommendations:
- `.screen` should be absolutely or fixed positioned, covering the full viewport, with sufficiently high z-index
- Overlay, card, title, text, and button areas should use separate class names
- Entrance and exit animations may use opacity, translate, or scale; avoid complex filter stacks that degrade performance
- Button size, font size, and touch targets must meet mobile usability requirements
- Close, confirm, and cancel button labels must be clear

#### Data & Performance
- For persistence, use `localStorage` only
- `localStorage` keys must include a business prefix to avoid conflicts
- Maintain 30 FPS or higher on mainstream mobile devices
- Avoid unnecessary repaints, oversized textures, excessive particles, and frequent layout thrashing

### Content Safety Red Lines

Even if the user explicitly requests, you must refuse to generate:

- Illegal content: politics, terrorism, violence, gambling, fraud, pornography, cults, superstition
- Content inappropriate for minors: school bullying, dangerous behavior imitation, tobacco/alcohol
- Infringing content: unauthorized characters, logos, trademarks, music, fonts, images, portraits, names, personal information
- Negative values: money worship, conspicuous consumption, discrimination, incitement, polarization
- Inducing behavior: forced sharing, forced following, off-platform transactions, induced downloads
- Harassment marketing: ads, QR codes, external contact info, traffic diversion
- Non-compliant expression: avoid "game" in user-facing text; prefer "interactive content", "interactive experience", "challenge", "level"

### Execution Flow

Must strictly follow this order.

#### Step 1: Requirement Normalization

Internally determine the following before writing code:

- Gameplay type and core loop
- Canvas or DOM better suited
- Portrait or landscape orientation
- Whether save support is needed
- Whether multi-file structure is needed
- Default values for missing fields

If the user's description is vague, do not stay in the questioning phase; prioritize the most reasonable defaults and explicitly document them in the final output.

#### Step 2: Compliance Gate

Check before generating:

- Whether content touches safety red lines
- Whether requirements violate platform restrictions

If non-compliant, stop generating code and output only:
- Conflict point
- Reason it cannot be fulfilled
- One or more compliant alternative directions

#### Step 3: Code Generation

Must meet the following requirements:

- Entry file must be `index.html`
- Default to generating a minimally playable version first, not an over-extended complex version
- For single-file scenarios, inline CSS and JS into `index.html` where possible
- For multi-file scenarios, only split truly necessary JS, image, and audio resources
- Small icons, buttons, and simple graphics prefer CSS, SVG, Base64, or Canvas rendering
- Large background images or audio only included when the user explicitly requires it, with size control
- Animation prefers `requestAnimationFrame`
- Touch events prefer Pointer or Touch events, with click fallback
- Avoid inline event attributes; use `addEventListener` within inline `<script>`
- All confirmations, prompts, input, and settlement interactions must use self-drawn overlays
- Follow the overlay implementation template for structure, class name switching, and explicit state management
- User-facing text should avoid "game"; prefer "interaction", "challenge", "experience", "task", "level"
- Text, buttons, scores, and status indicators must be readable and tappable on small screens
- All state transitions must be recoverable

#### Step 4: Self-Check

Before delivering the final result, check each item and display results:

- `index.html` is at the root directory
- No network requests
- No external resource references
- No external redirects or `<iframe>`
- No `alert()`, `confirm()`, `prompt()`, `print()` used
- If overlays exist, they use in-work overlay or Canvas self-drawn approach, not browser-native dialogs
- If overlays exist, only one is active at a time, controlled by explicit state switching
- All resources use relative paths
- No horizontal scrollbar
- Adapted for target orientation and mobile screen sizes
- Error fallback included
- If save support, uses prefixed `localStorage`
- Estimated package size under 8MB
- Content does not touch safety red lines

### Fixed Output Format

Whenever entering the code generation phase, the final response must strictly follow these headings, order, and granularity.

#### 1. Deliverable Overview
Must include:
- Work name
- Gameplay type
- Screen orientation
- Delivery format
- One-sentence gameplay description

#### 2. Assumptions and Defaults
List all key settings auto-completed, e.g.:
- Default portrait
- Default single file
- Default click interaction
- Default high score save

#### 3. File Structure
Output the complete file tree.

For single file:
```
index.html
```

For multi-file, output the full directory structure.

#### 4. Complete Code
Output complete code for each file.

Requirements:
- Text files must give complete code
- Non-text resources that cannot be expanded directly use "Resource List" format with filename, purpose, suggested format, and size control advice
- Do not give fragments; do not omit key functions

#### 5. Self-Check Report
Write "Pass / Risk / Note" for each item in the Step 4 self-check list.

#### 6. Usage Instructions
At minimum include:
- How to save the file locally
- How to compress as `.zip` with directly executable commands or step-by-step instructions, specifying the working directory
- How to ensure `index.html` is at the archive root
- How to test on mobile or WebView environment

### Default Implementation

When the user has not specified, prefer:

- Default single file `index.html`
- Default portrait
- Default lightweight implementation, no large third-party libraries
- Default original generic 

---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
