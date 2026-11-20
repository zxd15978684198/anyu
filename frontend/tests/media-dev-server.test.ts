/**
 * dev server `/media/**` 的守卫用例。
 *
 * 为什么值得写：这条链路的退化**不会报错**。`/media/opera/x.mp3` 一旦没人接，vite 的
 * SPA fallback 会回 200 + index.html，`<audio>` 拿着 HTML 解析失败，表现只是「生产能放、
 * 本地不响」；路径穿越更糟 —— 它会把仓库根 `.env` 当静态文件吐出去，日志里什么都看不出来。
 * 所以这里起一个**真实的 vite dev server**（port 0，不占 5173），用裸 HTTP 请求验四件事：
 * 200 + `audio/mpeg`、找不到就 404（不回退 HTML）、`..` 与编码/反斜杠变体一律非 200、
 * HEAD 不带 body。
 *
 * 为什么不用 `readFileSync(vite.config.ts)` 断字符串：那种用例改注释都能过，证明不了
 * middleware 真的挂在了 fallback 之前 —— 而这正是本 bug 的全部内容。
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { request as httpRequest } from "node:http";
import { existsSync, mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const FRONTEND_DIR = fileURLToPath(new URL("..", import.meta.url));
const CONFIG_FILE = fileURLToPath(new URL("../vite.config.ts", import.meta.url));
const AUDIO_FILE = fileURLToPath(new URL("../../media/opera/peking-sand-bar.mp3", import.meta.url));

/**
 * 「200 + audio/mpeg」这条断言需要一个**确实存在于磁盘**的 mp3。
 *
 * 但 `.gitignore:84` 明确只把 `media/` 下的音频扩展名留在本机（入库的事实来源是
 * `media/manifest.json`），CI 检出里没有任何音频文件 —— 于是这条用例在 CI 上必然 404。那不是中间件坏了，是夹具缺件。
 * 所以缺件时现场造一个 1 KB 的假 mp3（content-type 由扩展名决定），用例结束就删掉；
 * 本机有真素材时仍走真素材，不改变本地的验证强度。
 */
function servableAudio(sub: TestContext): { relative: string; absolute: string } {
  if (existsSync(AUDIO_FILE)) {
    return { relative: "opera/peking-sand-bar.mp3", absolute: AUDIO_FILE };
  }
  const dir = fileURLToPath(new URL("../../media/opera/", import.meta.url));
  const absolute = join(dir, "__test-fixture.mp3");
  mkdirSync(dir, { recursive: true });
  writeFileSync(absolute, Buffer.alloc(1024, 0x7f));
  sub.after(() => rmSync(absolute, { force: true }));
  return { relative: "opera/__test-fixture.mp3", absolute };
}

interface RawResponse {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: Buffer;
}

/** 用 `http.request` 而不是 `fetch`：`fetch`/`URL` 会把路径里的 `..` 规范化掉，测不到穿越。 */
function rawRequest(port: number, rawPath: string, method = "GET"): Promise<RawResponse> {
  return new Promise((resolvePromise, rejectPromise) => {
    const req = httpRequest(
      { host: "127.0.0.1", port, path: rawPath, method },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () =>
          resolvePromise({ status: res.statusCode ?? 0, headers: res.headers, body: Buffer.concat(chunks) }),
        );
      },
    );
    req.on("error", rejectPromise);
    req.end();
  });
}

test("dev server 提供 /media/**，且挡住路径穿越", async (t) => {
  const server = await createServer({
    root: FRONTEND_DIR,
    configFile: CONFIG_FILE,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false, open: false, hmr: false },
  });
  t.after(async () => {
    await server.close();
  });
  await server.listen();

  const address = server.httpServer?.address();
  assert.ok(address && typeof address === "object", "dev server 未监听 TCP");
  const port = address.port;

  await t.test("存在 200 + audio/mpeg + 真实字节", async (sub) => {
    const audio = servableAudio(sub);
    const res = await rawRequest(port, `/media/${audio.relative}`);
    assert.equal(res.status, 200);
    assert.equal(res.headers["content-type"], "audio/mpeg");
    assert.equal(res.body.length, statSync(audio.absolute).size);
  });

  await t.test("不存在 404，不回退 index.html", async () => {
    const res = await rawRequest(port, "/media/opera/nope.mp3");
    assert.equal(res.status, 404);
    assert.doesNotMatch(String(res.headers["content-type"] ?? ""), /text\/html/);
  });

  await t.test("HEAD 只回头不带 body", async (sub) => {
    const audio = servableAudio(sub);
    const res = await rawRequest(port, `/media/${audio.relative}`, "HEAD");
    assert.equal(res.status, 200);
    assert.equal(res.headers["content-type"], "audio/mpeg");
    assert.equal(res.body.length, 0);
  });

  await t.test("穿越：斜杠 / 编码点 / 反斜杠一律非 200", async () => {
    const attempts = ["/media/../../.env", "/media/%2e%2e/%2e%2e/.env", "/media/..%5c..%5c.env"];
    for (const rawPath of attempts) {
      const res = await rawRequest(port, rawPath);
      assert.notEqual(res.status, 200, `${rawPath} 不应被当作静态文件返回`);
      assert.doesNotMatch(res.body.toString("utf8"), /DEEPSEEK|SECRET|DATABASE_URL/i);
    }
  });

  await t.test("非 /media 路径照旧走 vite", async () => {
    const res = await rawRequest(port, "/pad-does-not-exist");
    assert.equal(res.status, 200);
    assert.match(String(res.headers["content-type"] ?? ""), /text\/html/);
  });
});
