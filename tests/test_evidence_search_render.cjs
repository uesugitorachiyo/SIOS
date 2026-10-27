// Run with: node --test tests/test_evidence_search_render.cjs
// Exercises the renderer from the shipped HTML using only Node's standard library.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../src/aios/evidence_search_ui.html'), 'utf8');
const start = html.indexOf('function renderSourceText(');
const end = html.indexOf('function showSource(', start);
assert(start >= 0 && end > start, 'source renderer must be present in the packaged HTML');

class Element {
  constructor(tag, text = '', className = '') {
    this.tag = tag; this.textContent = text; this.className = className;
    this.children = []; this.attributes = {}; this.handlers = {}; this.disabled = false;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  click() { if (!this.disabled) this.handlers.click?.(); }
}

function render(text) {
  const parent = new Element('div');
  const add = (node, tag, value, className) => {
    const element = new Element(tag, value, className); node.children.push(element); return element;
  };
  const context = { add };
  vm.runInNewContext(html.slice(start, end), context);
  context.renderSourceText(parent, text);
  return parent;
}

test('small and empty originals are displayed unchanged without pagination', () => {
  for (const value of ['', 'Small <script>literal</script> source.', 'x'.repeat(32000)]) {
    const result = render(value);
    assert.equal(result.children.length, 1);
    assert.equal(result.children[0].tag, 'pre');
    assert.equal(result.children[0].textContent, value);
  }
});

test('all parts reconstruct the original and previous and next honor boundaries', () => {
  const original = 'A'.repeat(31999) + '😀' + 'B'.repeat(64002);
  const result = render(original), [nav, pre] = result.children;
  const [previous, status, next] = nav.children;
  assert.equal(nav.attributes['aria-label'], 'Original source parts');
  assert.equal(status.attributes['aria-live'], 'polite');
  assert.equal(previous.disabled, true);
  assert.match(status.textContent, /Part 1 of 4/);
  assert.equal(pre.textContent.length, 31999);
  let reconstructed = pre.textContent;
  pre.scrollTop = 400;
  next.click();
  assert.equal(pre.scrollTop, 0);
  reconstructed += pre.textContent;
  while (!next.disabled) { next.click(); reconstructed += pre.textContent; }
  assert.equal(reconstructed, original);
  assert.match(status.textContent, /Part 4 of 4/);
  assert.equal(next.disabled, true);
  previous.click();
  assert.equal(next.disabled, false);
  assert.match(status.textContent, /Part 3 of 4/);
  while (!previous.disabled) previous.click();
  assert.equal(pre.textContent, original.slice(0, 31999));
});

test('the renderer does not split a surrogate pair at the boundary', () => {
  const original = 'x'.repeat(31999) + '😀' + 'z';
  const [nav, pre] = render(original).children;
  assert.equal(pre.textContent, 'x'.repeat(31999));
  nav.children[2].click();
  assert.equal(pre.textContent, '😀z');
});
