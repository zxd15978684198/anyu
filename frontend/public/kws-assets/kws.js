/**
 * sherpa-onnx KWS 的浏览器薄胶水（ESM，运行期从 /kws-assets 静态注入）。
 *
 * 为什么要有这一层：
 * 1. 上游编译产物（sherpa-onnx-kws-runtime.js）是压缩后的 emscripten 胶水，
 *    对外只导出 `n` / `t` 两个混淆名；中文符号名只在这里出现一次，便于审计。
 * 2. 模型权重不进 wasm，必须由调用方把 preload.data 按 metadata 的偏移写进
 *    emscripten 虚拟文件系统（等价于 @sherpaw/preloader 的 loadData，这里自实现
 *    以避免引入 npm 依赖）；FS_createDataFile 由 emscripten 运行时导出。
 *
 * 资产来源与许可证见同目录 NOTICE.md（Apache-2.0）。
 */

import {
  n as createKeywordSpotter,
  t as initKWSModule,
} from "./sherpa-onnx-kws-runtime.js";

/**
 * 把 emscripten file_packager 打出的模型包挂进虚拟文件系统。
 * @param {object} module initKWSModule() 的返回值（含 FS_createDataFile）
 * @param {ArrayBuffer | Uint8Array} data preload.data 全量字节
 * @param {{files: Array<{filename: string, start: number, end: number}>}} metadata
 * @returns {string[]} 实际写入的虚拟路径
 */
export function loadModelPack(module, data, metadata) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  const written = [];
  for (const { filename, start, end } of metadata.files) {
    module.FS_createDataFile("/", filename, bytes.subarray(start, end), true, true, true);
    written.push(filename);
  }
  return written;
}

export { createKeywordSpotter, initKWSModule };
