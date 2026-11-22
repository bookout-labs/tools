import { readFileSync, realpathSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(realpathSync(process.argv[2]));
const { renderMermaid } = await import(pathToFileURL(require.resolve("./index.js")));
const puppeteer = require("puppeteer");
const { drawings, mermaidConfig, puppeteerConfig, output } = JSON.parse(readFileSync(0, "utf8"));
const browser = await puppeteer.launch({ headless: "shell", ...puppeteerConfig });

try {
  const page = await browser.newPage();
  const collisions = [];
  let index = 0;
  for (const [name, definition] of Object.entries(drawings)) {
    const { data } = await renderMermaid(browser, definition, "svg", { mermaidConfig });
    await page.setContent(Buffer.from(data).toString("utf8"));
    const overlaps = await page.evaluate(async () => {
      await document.fonts.ready;
      const boxes = (selector, idOf) => [...document.querySelectorAll(selector)].map(element => ({
        id: idOf(element),
        text: element.textContent.trim(),
        rect: element.getBoundingClientRect(),
      }));
      const labels = boxes("g.edgeLabel > g[data-id]", element => element.dataset.id)
        .filter(({ rect }) => rect.width && rect.height);
      const nodes = boxes("g.node", element => element.id.split("-").at(-2));
      const intersects = (a, b) =>
        a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
      const overlaps = labels.flatMap((label, i) =>
        [...labels.slice(i + 1), ...nodes]
          .filter(other => intersects(label.rect, other.rect))
          .map(other => `${JSON.stringify(label.text)} overlaps ${JSON.stringify(other.text)}`));

      for (const path of document.querySelectorAll("path.flowchart-link[data-edge]")) {
        const id = path.dataset.id;
        // Mermaid embeds the generated n<number> node IDs in each edge ID.
        const [, source, target] = id.split("_");
        const bounds = path.getBoundingClientRect();
        const obstacles = [...labels, ...nodes].filter(box =>
          box.id !== id && box.id !== source && box.id !== target && intersects(bounds, box.rect));
        if (!obstacles.length) continue;

        const matrix = path.getScreenCTM();
        const length = path.getTotalLength();
        const steps = Math.max(1, Math.ceil(length));
        const points = Array.from({ length: steps + 1 }, (_, i) =>
          path.getPointAtLength(length * i / steps).matrixTransform(matrix));
        const label = labels.find(box => box.id === id)?.text ??
          `${nodes.find(box => box.id === source).text} → ${nodes.find(box => box.id === target).text}`;
        for (const { text, rect } of obstacles) {
          if (points.some(({ x, y }) => x > rect.left && x < rect.right && y > rect.top && y < rect.bottom)) {
            overlaps.push(`connector ${JSON.stringify(label)} crosses ${JSON.stringify(text)}`);
          }
        }
      }
      return overlaps;
    });
    collisions.push(...overlaps.map(overlap => `${name}: ${overlap}`));
    writeFileSync(join(output, `rendered-${++index}.svg`), data);
  }
  if (collisions.length) throw new Error(collisions.join("\n"));
  console.log(`SVG overlap check passed for ${index} diagrams.`);
} finally {
  await browser.close();
}
