#!/usr/bin/env node
/*
 * Static checks for the frontend in public/ (no browser needed):
 *   - every <script> block and every .js file parses as JavaScript;
 *   - no page builds a WebSocket URL by hand or embeds a host/port: all three
 *     applets must go through LightsailDemo.DemoSocket (wss on https pages);
 *   - the chat page never assigns innerHTML (rendering is text-node only);
 *   - the game never loads a peer-supplied image URL;
 *   - no inline event handlers (onclick=...) remain.
 * Exit status 1 with a list of problems on failure.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const publicDir = path.join(__dirname, '..', 'public');
const problems = [];

function read(rel) {
  return fs.readFileSync(path.join(publicDir, rel), 'utf8');
}

function checkSyntax(label, source) {
  try {
    // eslint-disable-next-line no-new-func
    new Function(source);
  } catch (err) {
    problems.push(`${label}: JavaScript syntax error: ${err.message}`);
  }
}

function inlineScripts(html) {
  const out = [];
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
  let match;
  while ((match = re.exec(html)) !== null) out.push(match[1]);
  return out;
}

const pages = ['index.html', 'chat/index.html', 'draw/index.html', 'game/index.html'];
const applets = ['chat/index.html', 'draw/index.html', 'game/index.html'];

for (const rel of pages) {
  const html = read(rel);
  inlineScripts(html).forEach((src, i) => checkSyntax(`${rel} <script #${i + 1}>`, src));
  if (/\son[a-z]+\s*=/i.test(html)) problems.push(`${rel}: inline event handler attribute found`);
  if (/\.innerHTML\s*[+]?=/.test(html)) problems.push(`${rel}: innerHTML assignment found (use textContent)`);
  if (/new\s+WebSocket\s*\(/.test(html)) problems.push(`${rel}: direct WebSocket construction (use LightsailDemo.DemoSocket)`);
  if (/\bws:\/\/|\bwss:\/\//.test(html)) problems.push(`${rel}: hard-coded ws:// or wss:// URL`);
  if (/\b\d{1,3}(\.\d{1,3}){3}\b/.test(html)) problems.push(`${rel}: embedded IP address`);
  if (/:8101\b/.test(html)) problems.push(`${rel}: private backend port embedded`);
}

for (const rel of applets) {
  const html = read(rel);
  if (!html.includes('/shared/demo-socket.js')) problems.push(`${rel}: does not load /shared/demo-socket.js`);
  if (!html.includes('/shared/demo.css')) problems.push(`${rel}: does not load /shared/demo.css`);
  if (!/LightsailDemo\.DemoSocket\('\/ws\/(chat|draw|game)'/.test(html)) problems.push(`${rel}: does not create a DemoSocket for its /ws/ path`);
}

const game = read('game/index.html');
if (/playerImg\.src\s*=\s*player\./.test(game) || /\.src\s*=\s*msg\.image/.test(game)) {
  problems.push('game/index.html: peer-supplied image URL is assigned to an Image');
}
if (!/sprite:\s*mySprite/.test(game)) problems.push('game/index.html: movement message must send the sprite name');

const shared = read('shared/demo-socket.js');
checkSyntax('shared/demo-socket.js', shared);
if (!shared.includes("window.location.protocol === 'https:' ? 'wss' : 'ws'")) {
  problems.push('shared/demo-socket.js: scheme must follow the page (wss on https, ws on http)');
}
if (!shared.includes('window.location.host')) problems.push('shared/demo-socket.js: host must come from the page');
if (/setInterval|setTimeout\([^)]*connect/.test(shared)) problems.push('shared/demo-socket.js: automatic reconnect loop found; reconnect is manual');

for (const rel of fs.readdirSync(path.join(publicDir, 'shared'))) {
  if (rel.endsWith('.js')) checkSyntax(`shared/${rel}`, read(`shared/${rel}`));
}

if (problems.length) {
  console.error('frontend checks failed:');
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(`frontend checks ok (${pages.length} pages, shared helper)`);
