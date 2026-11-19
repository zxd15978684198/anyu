import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

/** 演示门户入口（portal.html，第 4 个 MPA 入口；前端计划 §10 批次 WP）。
 *  不占用 `/`：根地址仍是长者端，保住「开机即数字人」的设备语义。 */
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);