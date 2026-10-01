#!/usr/bin/env node
/**
 * 用本机 Chrome 的 CDP 给面板截图（无需 Playwright/Puppeteer）。
 *
 *   node tools/cdp_screenshot.mjs --url http://127.0.0.1:8010/panel/ --out tools/panel-screenshot.png
 *
 * 说明：脚本自带硬超时（默认 45s）与沙箱兼容参数，失败会以非零码退出并打印诊断。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const CANDIDATES = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);

const argvOf = (name, fallback) => {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback;
};

const url = argvOf("--url", "http://127.0.0.1:8010/panel/");
const out = resolve(argvOf("--out", "tools/panel-screenshot.png"));
const width = Number(argvOf("--width", "1500"));
const height = Number(argvOf("--height", "1250"));
const settleMs = Number(argvOf("--wait", "5000"));
const hardTimeoutMs = Number(argvOf("--timeout", "45000"));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const deadline = Date.now() + hardTimeoutMs;

async function withTimeout(promise, ms, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, rej) => {
        timer = setTimeout(() => rej(new Error("超时: " + label)), ms);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function findBrowser() {
  for (const candidate of CANDIDATES) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("找不到 Chrome/Edge，请用 CHROME_PATH 指定可执行文件");
}

async function fetchJson(path, tries) {
  for (let i = 0; i < tries; i++) {
    if (Date.now() > deadline) throw new Error("CDP 等待超过硬超时: " + path);
    try {
      const res = await fetch(path);
      if (res.ok) return await res.json();
    } catch (err) {
      void err;
    }
    await sleep(200);
  }
  throw new Error("CDP 端口未就绪: " + path);
}

const browserPath = findBrowser();
const port = 9600 + Math.floor(Math.random() * 300);
const profile = mkdtempSync(join(tmpdir(), "panel-shot-"));
const child = spawn(
  browserPath,
  [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--hide-scrollbars",
    "--remote-debugging-port=" + port,
    "--user-data-dir=" + profile,
    "about:blank",
  ],
  { stdio: "ignore", windowsHide: true }
);

let exitCode = 0;
try {
  await withTimeout(fetchJson("http://127.0.0.1:" + port + "/json/version", 100), 25000, "CDP 启动");
  const list = await withTimeout(fetchJson("http://127.0.0.1:" + port + "/json/list", 20), 8000, "CDP tab 列表");
  const target = list.find((t) => t.type === "page") || list[0];
  if (!target || !target.webSocketDebuggerUrl) throw new Error("拿不到可调试的 page target");

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  const pending = new Map();
  const events = [];
  let nextId = 1;

  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      const entry = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(JSON.stringify(msg.error)));
      else entry.resolve(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  });
  await withTimeout(
    new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", () => rej(new Error("WebSocket 连接失败")), { once: true });
    }),
    8000,
    "WebSocket 连接"
  );

  const send = (method, params) =>
    withTimeout(
      new Promise((res, rej) => {
        const id = nextId++;
        pending.set(id, { resolve: res, reject: rej });
        ws.send(JSON.stringify({ id, method, params: params || {} }));
      }),
      15000,
      method
    );

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: width,
    height: height,
    deviceScaleFactor: 2,
    mobile: false,
  });
  await send("Page.navigate", { url: url });
  await sleep(settleMs);

  const expression = [
    "JSON.stringify({",
    "  title: document.title,",
    "  cards: document.querySelectorAll('#cards .card').length,",
    "  cardValues: Array.from(document.querySelectorAll('#cards .value')).map(function(e){return e.textContent;}),",
    "  tasks: document.querySelectorAll('#tasks .task').length,",
    "  bars: document.querySelectorAll('#chart-queries rect').length,",
    "  latencyLines: document.querySelectorAll('#chart-latency polyline').length,",
    "  eventRows: document.querySelectorAll('#events tbody tr').length,",
    "  notifyRows: document.querySelectorAll('#notify-body tbody tr').length,",
    "  logLines: document.querySelectorAll('#logbox .l').length,",
    "  banner: (document.getElementById('risk-banner')||{}).textContent,",
    "  pills: Array.from(document.querySelectorAll('header .pill')).map(function(e){return e.textContent.trim();}),",
    "  denied: document.body.innerText.indexOf('panel_access_denied') >= 0",
    "})",
  ].join("\n");
  const probe = await send("Runtime.evaluate", { expression: expression, returnByValue: true });

  const errors = events
    .filter((e) => e.method === "Log.entryAdded" && e.params.entry.level === "error")
    .map((e) => e.params.entry.text);
  const exceptions = events
    .filter((e) => e.method === "Runtime.exceptionThrown")
    .map((e) => (e.params.exceptionDetails.exception || {}).description || e.params.exceptionDetails.text);

  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, Buffer.from(shot.data, "base64"));

  console.log("URL       : " + url);
  console.log("截图      : " + out);
  console.log("探测结果  : " + probe.result.value);
  console.log("控制台错误: " + (errors.length ? JSON.stringify(errors) : "(无)"));
  console.log("JS 异常   : " + (exceptions.length ? JSON.stringify(exceptions) : "(无)"));
  if (exceptions.length || errors.length) exitCode = 2;
  ws.close();
} catch (error) {
  console.error("截图失败: " + error.message);
  exitCode = 1;
} finally {
  try {
    child.kill();
  } catch (err) {
    void err;
  }
}
process.exit(exitCode);
