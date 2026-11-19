/**
 * 数字人舞台层（2026-11-11）：**在欢迎页（登录页）就挂载，登录后原地继续**。
 *
 * 为什么提到 App 根层：把 10.7MB VRM 解析、纹理上传、MToon 着色器编译这三笔开销
 * 挪到「用户还在输设备号/看登录卡片」的那十几秒里付掉；登录进主界面时画面已经在那儿，
 * 不需要再挂载一次（此前 MainScreen 挂载 → 首帧 1.4–3.7s，用户看到的就是「还要加载」）。
 *
 * 几何：与主界面中部左栏完全重合（顶栏之下、底栏之上、字幕右栏以左），
 * 所以从登录页切到主界面时画布**不移动、不重排**，只是后面的文字层换了。
 * 四条边都由 CSS 变量给（`--pad-stage-top/bottom/left/right`，见 index.css），
 * 其中上下两条跟着顶栏/底栏的**实测**高度走（layoutBands.ts），断点改一处即可。
 * 左右还要加 `--pad-shell-inset`：超宽档整页封顶居中，而这层是 fixed（相对视口），
 * 不补居中量就会比舞台左偏（2560 上实测 80px）。
 * 堆叠档（手机横屏、平板/手机竖屏）把字幕挪到下方，靠 `--pad-stage-bottom` 让位。
 *
 * 指针事件一律穿透：这张层只负责显示，按钮/输入框都在它上层的文档流里。
 */
import { Suspense } from "react";

import { AvatarBoundary } from "./AvatarBoundary";
import { AvatarStage } from "./AvatarStage";
import { attachAvatarController } from "./avatarHost";


export interface AvatarLayerProps {
  /**
   * 是否水平居中。登录页表单在正中，人若也居中就会从输入框后面穿出来 —— 未登录时靠左站。
   * 登录后主界面左栏本来就是居中区，恢复居中即可（画布位置变化只在这两个状态间切一次）。
   */
  centered?: boolean;
  /** 端侧诊断（vrm_load / avatar_ready）上报，登录前 client 为 null，中转站会丢弃 */
  onDiag?: (d: { stage: "vrm_load" | "avatar_ready"; ok: boolean; error?: string }) => void;
  /**
   * 是否可见。**登录页不显示数字人**（2026-11-13 产品负责人要求）：还没进家门就先看见「人」，
   * 对长者是叙事错误——她应该是「登录之后才被叫醒」。
   *
   * 但**不卸载**：这个层的价值就是把 VRM 解析、纹理上传、MToon 编译挪到登录那十几秒里
   * 付掉（见文件头注释），卸载就白等了。所以这里只切 opacity，DOM 与 rAF 都留着。
   */
  visible?: boolean;
}

export function AvatarLayer({ onDiag, centered = true, visible = true }: AvatarLayerProps) {
  return (
    <div
      // 长者端单人单舞台；P/PV 档把 3D 隐去（--pad-avatar-opacity: 0），但**不卸载**——
      // 卸载会打断 rAF、断掉 avatar_ready 上报，也让回到 D 档要重新解析一次模型。
      // 四条边全走变量：堆叠档（手机/平板竖屏）改的是 --pad-stage-*，不是这里。
      //
      // z-10：`.pad-stage` 的聚光渐变是**相对定位的普通文档元素**，按绘制顺序会盖在
      // fixed z-0 的画布之上（2026-11-16 实测 elementFromPoint 命中的就是 .pad-stage），
      // 长者看到的就是「人前面一层白雾」。抬到 z-10 后渐变退到画布背后当舞台底；
      // 仍低于任务抽屉（z-20）与各浮层（z-30/40），不会盖住交互。
      className="pointer-events-none fixed z-10 flex items-center justify-start"
      style={{
        top: "var(--pad-stage-top)",
        bottom: "var(--pad-stage-bottom)",
        left: "calc(var(--pad-stage-left) + var(--pad-shell-inset))",
        right: "calc(var(--pad-stage-right) + var(--pad-shell-inset))",
        opacity: visible ? "var(--pad-avatar-opacity)" : 0,
        // 登录页：人靠左站（表单在中间）；登录后：回到左栏正中
        justifyContent: centered ? "center" : "flex-start",
        paddingLeft: centered ? 0 : "calc(var(--pad-gutter-x) * 2)",
      }}
      data-testid="avatar-layer"
      aria-hidden="true"
    >
      <AvatarBoundary>
        <Suspense fallback={null}>
          <AvatarStage
            onDiag={(d) => onDiag?.(d)}
            onReady={(controller) => { if (controller) attachAvatarController(controller); }}
          />
        </Suspense>
      </AvatarBoundary>
    </div>
  );
}
