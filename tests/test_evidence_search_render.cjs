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

test('result titles prefer the source title over generic headings and opaque selective IDs', () => {
  const context = {};
  vm.runInNewContext(html.slice(html.indexOf('function sourceTitle('), start), context);
  assert.equal(context.sourceTitle({path:'Brain/example.md',text:'---\ntitle: My goal\n---\n# Goal\n'}), 'My goal');
  assert.equal(context.sourceTitle({path:'Memory/x.md',text:'---\ntitle: selective-' + 'a'.repeat(64) + '\n---\n# Useful note\n'}), 'Useful note');
  assert.equal(context.sourceSnippet('one exact a+b example', ['a+b']), 'one exact a+b example');
});

test('filter reports loaded matches without promising a nonexistent next page', () => {
  const start = html.indexOf('function applyFilter()');
  const end = html.indexOf("filter.addEventListener", start);
  const rows = [{dataset:{store:'brain'}}, {dataset:{store:'memory'}}];
  const context = {results:{querySelectorAll:()=>rows}, filter:{value:'brain'},
                   filterStatus:{textContent:''}, loaded:2, nextOffset:null};
  vm.runInNewContext(html.slice(start, end), context);
  context.applyFilter();
  assert.equal(rows[0].hidden, false); assert.equal(rows[1].hidden, true);
  assert.equal(context.filterStatus.textContent, '1 of 2 loaded results shown.');
  context.nextOffset=30; context.applyFilter();
  assert.match(context.filterStatus.textContent, /Load more/);
});

class Element {
  constructor(tag, text = '', className = '') {
    this.tag = tag; this.textContent = text; this.className = className;
    this.children = []; this.attributes = {}; this.handlers = {}; this.disabled = false;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  click() { if (!this.disabled) this.handlers.click?.(); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = nodes.map(node => node.textContent).join(''); }
  scrollIntoView() {}
}

function render(text) {
  const parent = new Element('div');
  const add = (node, tag, value, className) => {
    const element = new Element(tag, value, className); node.children.push(element); return element;
  };
  const document = { createElement: tag => new Element(tag), createTextNode: text => new Element('#text', text) };
  const context = { add, document };
  vm.runInNewContext(html.slice(start, end), context);
  context.renderSourceText(parent, text);
  return parent;
}

test('small and empty originals are displayed unchanged', () => {
  for (const value of ['', 'Small <script>literal</script> source.', 'x'.repeat(32000)]) {
    const result = render(value);
    assert.equal(result.children.length, 3);
    assert.equal(result.children[2].tag, 'pre');
    assert.equal(result.children[2].textContent, value);
  }
});

test('all parts reconstruct the original and previous and next honor boundaries', () => {
  const original = 'A'.repeat(31999) + '😀' + 'B'.repeat(64002);
  const result = render(original), [, nav, pre] = result.children;
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
  const [, nav, pre] = render(original).children;
  assert.equal(pre.textContent, 'x'.repeat(31999));
  nav.children[2].click();
  assert.equal(pre.textContent, '😀z');
});

test('finds literal text in later original parts and cycles matching parts', () => {
  const original = 'a'.repeat(32000) + 'First a+b' + 'b'.repeat(31991) + 'Second A+B';
  const [finder, nav, pre] = render(original).children;
  const [, field, find, message] = finder.children;
  field.value = 'a+b'; find.click();
  assert.match(pre.textContent, /First a\+b/);
  assert.equal(pre.children[1].tag, 'mark');
  assert.equal(pre.children[1].textContent, 'a+b');
  assert.match(message.textContent, /Matching part 2 of 3/);
  find.click();
  assert.match(pre.textContent, /Second A\+B/);
  assert.match(nav.children[1].textContent, /Part 3 of 3/);
  field.value = 'not present'; find.click();
  assert.equal(message.textContent, 'No matching part found.');
});
