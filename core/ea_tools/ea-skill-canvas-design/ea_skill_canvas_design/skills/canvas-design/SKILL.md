---
name: canvas-design
description: "Create original poster-quality art as a single .pdf or .png, in two steps: write a named design philosophy (.md), then render it on canvas. Use for posters, artwork, covers, and static visual pieces."
user_invocable: true
hidden: true
context: inline
---

# Canvas Design

Build a piece of visual art in exactly two steps. Step one writes a **design philosophy** (a small aesthetic movement) into an `.md` file. Step two expresses that philosophy as a single, museum-quality page in `.pdf` or `.png`. The result is roughly 90% visual design and 10% essential text.

## When to Use

- The user asks for a poster, a piece of art, a cover, a card, or any static visual object.
- The request is aesthetic rather than informational: "make something striking about X".
- The user wants a print-quality image and accepts one page as the deliverable.

## When NOT to Use

- Information-dense documents with real paragraphs → `docx`, `pdf`, `write-docs`.
- Slides, decks, or presentation pages → `pptx`, `theme-factory`, `slideshow`.
- Data pictures (charts, graphs) → `chart-visualization`, `beautiful-mermaid`.
- Generated imagery from a prompt → `generate_image`, `edit_image`. This skill is hand-crafted composition, not diffusion output.
- Infographics that must carry data → `theme-factory` + HTML/landing-page work.

## Tools You Use

| Task | Tool | How |
|------|------|-----|
| Write the philosophy file | `file_write` | `PHILOSOPHY.md` next to the output |
| Inspect available fonts | `terminal` | `ls canvas-fonts/` in this skill directory |
| Render PNG | `terminal` | Python + Pillow (`PIL`); `pip install pillow` if missing |
| Render PDF | `terminal` | Python + ReportLab (`pip install reportlab`) |
| Read back the result | `pdf` | `action: "extract_text"` on the PDF, or open the PNG |
| Check output exists/size | `terminal` | `ls -l` plus a quick pixel check with Pillow |

## Step 1 — Design Philosophy (.md)

Write a visual philosophy, not a layout template.

**Name the movement** in one or two words: "Concrete Poetry", "Chromatic Silence", "Geometric Silence", "Organic Systems", "Analog Meditation".

**Articulate it in 4-6 concise paragraphs**, covering how it manifests through space and form, color and material, scale and rhythm, composition and balance, and visual hierarchy.

Hard rules for the philosophy:

- **Avoid redundancy.** Cover each aspect once; do not repeat color-theory or spatial points unless you add real depth.
- **Minimal text is a value, not a suggestion.** Information lives in design, not in paragraphs.
- **Repeatedly stress craftsmanship.** The work must read as meticulously crafted, the product of deep expertise and painstaking attention, master-level execution. Say it several ways.
- **Leave room for interpretation.** Be specific about the aesthetic direction, concise enough that the render pass can make high-level interpretive choices.
- **Keep it generic.** Do not mention the specific subject of the artwork; the philosophy should be reusable anywhere.

### Deduce the subtle reference

Before drawing anything, extract the quiet conceptual thread from the original request. It should be a niche reference embedded in the art itself — sophisticated, never literal. Someone familiar with the subject should feel it; everyone else sees a masterful abstract composition. Think of a jazz musician quoting another song: only those who know catch it, but everyone enjoys the music.

## Step 2 — Express It on the Canvas

Even for a movie, game, or book request, keep the result sophisticated. Art, never cartoon.

The visual language to aim for: systematic observation, as if the piece were a plate from an imaginary science. Dense accumulation of marks, repeated elements, layered patterns that build meaning through patient repetition. Sparse, clinical typography and small reference markers. A simple phrase or detail anchored subtly, so the invisible subject appears documented with the reverence usually reserved for observable phenomena.

- **Fonts.** Use the real typefaces in `canvas-fonts/` — load them by path (Pillow `ImageFont.truetype`, ReportLab `pdfmetrics.registerFont`). Most of the time the type should be thin. Make typography part of the artwork: if the piece is abstract, bring the letterforms onto the canvas rather than typesetting them digitally.
- **Text is context, not content.** Whether that means whisper-quiet labels or bold typographic gestures depends on the subject. A punk venue poster can shout; a ceramics studio identity should not.
- **Nothing leaves the page.** Nothing overlaps. Every element sits inside the canvas with proper margins, breathing room, and clear separation. This is non-negotiable.
- **One page.** One highly visual, design-forward PDF or PNG unless the user asks for more.

### Render recipe

```python
# PNG with Pillow — draw at 2x then downsample for smooth edges
from PIL import Image, ImageDraw, ImageFont

SCALE, W, H = 2, 1000, 1414   # A4-ish portrait at ~100 dpi * 2
img = Image.new("RGB", (W * SCALE, H * SCALE), "#F4EFE6")
d = ImageDraw.Draw(img)
font = ImageFont.truetype("canvas-fonts/BigShoulders-Regular.ttf", 40 * SCALE)
# ... compose ...
img.resize((W, H), Image.LANCZOS).save("poster.png", dpi=(200, 200))
```

```python
# PDF with ReportLab — vector shapes stay crisp
from reportlab.pdfgen import canvas as rc
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

pdfmetrics.registerFont(TTFont("BS", "canvas-fonts/BigShoulders-Regular.ttf"))
c = rc.Canvas("poster.pdf", pagesize=(842, 1191))  # A4 portrait
# ... draw ...
c.save()
```

## Quality Checklist

- The piece looks like it took countless hours; nothing reads as generated in seconds.
- No overlap, no clipping, no text touching the page edge.
- One named movement, and the artifact actually obeys its own rules.
- Type is restrained, thin where appropriate, and loaded from `canvas-fonts/`.
- Palette is small and intentional; repetition does the heavy lifting.
- Original work only — never copy an existing artist's composition, or you risk a copyright problem.
- Both deliverables exist: the rendered `.pdf` or `.png` and the `PHILOSOPHY.md` behind it.

## Common Pitfalls

- Writing a layout spec instead of a philosophy — the render pass then has nothing to interpret.
- Letting text become paragraphs; the philosophy should never become prose.
- Using system fonts for a print piece when `canvas-fonts/` already ships display typefaces.
- Rendering at 1x and calling it high resolution; draw at 2x and downsample.
- Placing the subject label front and center instead of anchoring it subtly.
- Forgetting the philosophy file, so the artwork arrives without its reasoning.

## Anti-patterns

- Diffusion output passed off as this skill's work.
- A "poster" that is really a slide with a background.
- Repeating the same phrase about color theory three times to fill the philosophy.
- Multiple pages when one strong page was asked for.
