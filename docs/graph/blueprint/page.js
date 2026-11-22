const pages = [...document.querySelectorAll(".page")];
const links = [...document.querySelectorAll("nav a[href^='#']")];

function showPage() {
  const active = pages.find(page => "#" + page.id === location.hash) || pages[0];
  pages.forEach(page => { page.hidden = page !== active; });
  links.forEach(link => {
    if (link.hash === "#" + active.id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.querySelectorAll(".file-details[open]").forEach(item => { item.open = false; });
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", showPage);
showPage();

document.querySelectorAll(".file-chip").forEach(chip => {
  const details = chip.querySelector("details");
  const link = chip.querySelector("a");
  const trigger = chip.querySelector("summary");
  const open = () => {
    document.querySelectorAll(".file-details[open]").forEach(item => {
      if (item !== details) item.open = false;
    });
  };
  trigger.addEventListener("click", open);
  chip.addEventListener("pointerenter", event => {
    if (event.pointerType === "mouse") {
      open();
      details.open = true;
    }
  });
  chip.addEventListener("pointerleave", event => {
    if (event.pointerType === "mouse" && !details.contains(document.activeElement)) details.open = false;
  });
  if (!link) return;
  let pointerType = "";
  link.addEventListener("pointerdown", event => { pointerType = event.pointerType; });
  link.addEventListener("click", event => {
    if (event.detail && pointerType === "touch" && !details.open) {
      event.preventDefault();
      open();
      details.open = true;
    }
  });
});

document.addEventListener("pointerdown", event => {
  document.querySelectorAll(".file-details[open]").forEach(item => {
    if (!item.closest(".file-chip").contains(event.target)) item.open = false;
  });
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    document.querySelectorAll(".file-details[open]").forEach(item => { item.open = false; });
  }
});

document.querySelectorAll(".diagram").forEach(figure => {
  const canvas = figure.querySelector(".canvas");
  const image = canvas.querySelector("img");
  const pointers = new Map();
  let scale = 1, x = 0, y = 0, fitted = 0, changed = false;
  const paint = () => { image.style.transform = `translate(${x}px, ${y}px) scale(${scale})`; };

  function fit() {
    if (!image.naturalWidth || !canvas.clientWidth) return;
    image.style.width = `${image.naturalWidth}px`;
    fitted = Math.min((canvas.clientWidth - 24) / image.naturalWidth,
                      (canvas.clientHeight - 24) / image.naturalHeight, 1.25);
    scale = fitted;
    x = (canvas.clientWidth - image.naturalWidth * scale) / 2;
    y = (canvas.clientHeight - image.naturalHeight * scale) / 2;
    changed = false;
    paint();
  }

  function zoom(factor, cx = canvas.clientWidth / 2, cy = canvas.clientHeight / 2, dx = 0, dy = 0) {
    if (!fitted) return;
    const next = Math.max(fitted / 2, Math.min(fitted * 12, scale * factor));
    x = cx - (cx - x) * next / scale + dx;
    y = cy - (cy - y) * next / scale + dy;
    scale = next;
    changed = true;
    paint();
  }

  image.addEventListener("load", fit);
  new ResizeObserver(() => { if (!changed) fit(); }).observe(canvas);
  if (image.complete) fit();
  figure.querySelectorAll("[data-view]").forEach(button => {
    button.addEventListener("click", () => {
      if (button.dataset.view === "reset") fit();
      else zoom(button.dataset.view === "in" ? 1.3 : 1 / 1.3);
    });
  });
  const point = event => {
    const box = canvas.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  };
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    const location = point(event);
    zoom(event.deltaY < 0 ? 1.13 : 1 / 1.13, location.x, location.y);
  }, { passive: false });
  canvas.addEventListener("pointerdown", event => {
    if ((event.pointerType !== "touch" && event.button !== 0) || pointers.size === 2) return;
    pointers.set(event.pointerId, point(event));
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("dragging");
  });
  canvas.addEventListener("pointermove", event => {
    const previous = pointers.get(event.pointerId);
    if (!previous) return;
    const next = point(event);
    pointers.set(event.pointerId, next);
    if (pointers.size === 1) {
      x += next.x - previous.x;
      y += next.y - previous.y;
      changed = true;
      paint();
      return;
    }
    const other = [...pointers.entries()].find(([id]) => id !== event.pointerId)[1];
    const before = Math.hypot(previous.x - other.x, previous.y - other.y);
    if (!before) return;
    const after = Math.hypot(next.x - other.x, next.y - other.y);
    zoom(after / before, (previous.x + other.x) / 2, (previous.y + other.y) / 2,
         (next.x - previous.x) / 2, (next.y - previous.y) / 2);
  });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
    canvas.addEventListener(name, event => {
      pointers.delete(event.pointerId);
      if (!pointers.size) canvas.classList.remove("dragging");
    });
  }
  canvas.addEventListener("keydown", event => {
    const shift = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] }[event.key];
    if (shift) {
      event.preventDefault();
      x += shift[0];
      y += shift[1];
      changed = true;
      paint();
    } else if (["+", "=", "-", "0"].includes(event.key)) {
      event.preventDefault();
      if (event.key === "0") fit();
      else zoom(event.key === "-" ? 1 / 1.3 : 1.3);
    }
  });
});
