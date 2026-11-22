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
      const boxes = selector => [...document.querySelectorAll(selector)].map(element => ({
        text: element.textContent.trim(),
        rect: element.getBoundingClientRect(),
      }));
      const labels = boxes("g.edgeLabel").filter(({ rect }) => rect.width && rect.height);
      const nodes = boxes("g.node");
      const intersects = (a, b) =>
        a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
      return labels.flatMap((label, i) =>
        [...labels.slice(i + 1), ...nodes]
          .filter(other => intersects(label.rect, other.rect))
          .map(other => `${JSON.stringify(label.text)} overlaps ${JSON.stringify(other.text)}`));
    });
    collisions.push(...overlaps.map(overlap => `${name}: ${overlap}`));
    writeFileSync(join(output, `rendered-${++index}.svg`), data);
  }
  if (collisions.length) throw new Error(collisions.join("\n"));
  console.log(`SVG overlap check passed for ${index} diagrams.`);
} finally {
  await browser.close();
}
