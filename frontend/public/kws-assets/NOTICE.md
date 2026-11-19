# kws-assets · 唤醒词「小伴」运行时资产

> 状态：随 FR-UI-11 / AC-42 入库　｜　日期：2026-11-09

本目录是**静态注入**资产（与 `public/vad-assets/` 同思路）：不打进 JS bundle，由
`src/pad/audio/kws.ts` 在运行期按 `/kws-assets/...` 动态 import / fetch。

| 文件 | 体积 | 来源 | 许可证 |
| --- | --- | --- | --- |
| `prebuilt/kws.wasm` | 11.9 MB | `@sherpaw/kws@0.3.0` 的 `dist/prebuilt/kws.wasm`（standalone sherpa-onnx KWS WASM 构建） | Apache-2.0 |
| `sherpa-onnx-kws-runtime.js` | 100 KB | 同上包内 `dist/wasm-CP0rq9dg.js`，逐字节复制、未改动 | Apache-2.0 |
| `model/preload.data` | 12.5 MB | HuggingFace `moeru-ai/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` 的 `install/bin/wasm/preload.data` | Apache-2.0 |
| `model/preload.js.metadata` | 272 B | 同上（emscripten file_packager 的偏移表） | Apache-2.0 |
| `model/tokens.txt` | 2 KB | 从 `preload.data` 的 `/tokens.txt` 段原样取出，方便人工核对唤醒词 token | Apache-2.0 |

- 上游模型：`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20`（官方 release 为 Apache-2.0，
  pack 内 README 注明「未改动上游权重」）。中文/英文混合，端侧 CPU 推理。
- SHA-256（前 16 位）：

  | 文件 | SHA-256 前缀 |
  | --- | --- |
  | `prebuilt/kws.wasm` | `B75814D1F900CC99` |
  | `model/preload.data` | `4CA3AE0D147DF157` |
  | `sherpa-onnx-kws-runtime.js` | `122C98B161ABD39A` |

- 唤醒词「小伴」的 token 编码（`tokens.txt` 为拼音声母 + 带调韵母与 ARPAbet 混合词表）：
  `x` + `iǎo` + `b` + `àn`。
- 体积合计约 24.5 MB。这是本地 nginx / vite 直接托管的静态文件，首次进入才下载；
  不参与 `pnpm build` 的 JS 产物（`public/` 原样拷贝）。
- 获取路径说明：仓库所在网络无法直连 `github.com` / `raw.githubusercontent.com`
  （TLS 被重置），故 wasm 取自 npm registry、模型取自 HuggingFace 镜像
  `hf-mirror.com`；两者均为上游官方产物的原样下载，未做任何重编译或量化。
