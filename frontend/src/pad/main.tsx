import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { guardStaleBundle } from "./buildInfo";
import { warmUpAvatarModel } from "./avatar/preloadAvatar";
import "./index.css";

// 先跑守卫再挂载：旧 bundle 直接重载，避免长者看到一版「已经修过却仍然复现」的界面。
guardStaleBundle();

// 登录前就把数字人预热（2026-11-11 产品负责人要求「一打开页面就加载好」）：
// 形象字节 + three-vrm 分包都与身份无关，放在这里预热，登录后就不必再等 10.7MB。
// fire-and-forget，不 await：登录卡片的首次绘制不能被它拖慢。
warmUpAvatarModel();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
