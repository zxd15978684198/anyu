/**
 * 数字人角色数据层（W1 · FR-3D-03）。
 *
 * 契约唯一依据：`app/api/elders.py` 的 `GET /api/elders/personas`。
 * 同一条列表同时供「数字人角色」页与长者档案的形象下拉使用，不造第二套读口。
 * 本模块只读：角色资产的编辑/新增不在本批，写入口仍是 `PATCH /api/elders/{id}/persona`。
 */
import { apiGet } from "./http";
import { asString, asStringList } from "./staff";

export interface PersonaAsset {
  persona_id: string;
  name: string;
  avatar_url: string | null;
  voice_id: string;
  personality: string;
  greeting: string;
  tone_examples: string[];
  assigned_elders: number;
}

function toPersonaAsset(raw: unknown): PersonaAsset {
  const row = (raw ?? {}) as Record<string, unknown>;
  const assigned = Number(row.assigned_elders);
  return {
    persona_id: asString(row.persona_id),
    name: asString(row.name),
    avatar_url: typeof row.avatar_url === "string" && row.avatar_url ? row.avatar_url : null,
    voice_id: asString(row.voice_id),
    personality: asString(row.personality),
    greeting: asString(row.greeting),
    tone_examples: asStringList(row.tone_examples),
    assigned_elders: Number.isFinite(assigned) && assigned >= 0 ? assigned : 0,
  };
}

/** `GET /api/elders/personas`（admin / doctor）。 */
export async function listPersonaAssets(): Promise<PersonaAsset[]> {
  const raw = await apiGet<unknown>("/api/elders/personas");
  const body = (raw ?? {}) as Record<string, unknown>;
  const items = Array.isArray(body.items) ? body.items : [];
  return items.map(toPersonaAsset).filter((item) => item.persona_id !== "");
}

/**
 * 资产号：固定 UUID 主体恒为 `11111111`，直接截前 8 位会让 9 个角色看起来同一个编号。
 * 取末段后 4 位（…111111111201 → 1201），与 `scripts/persona_catalog.py` 的编号一一对应。
 */
export function personaAssetNo(personaId: string): string {
  const tail = personaId.trim().split("-").at(-1) ?? "";
  return tail.length >= 4 ? tail.slice(-4) : tail || "—";
}

/** 音色 ID → 人读名字；未知值原样回显，不用「未知音色」掩盖配置错误。 */
const VOICE_LABELS: Record<string, string> = {
  "zh-CN-XiaoxiaoNeural": "晓晓（女声）",
  "zh-CN-XiaoyiNeural": "晓伊（女声）",
  "zh-CN-YunjianNeural": "云健（男声）",
  "zh-CN-YunxiNeural": "云希（男声）",
  "zh-CN-YunxiaNeural": "云夏（男声）",
  "zh-CN-YunyangNeural": "云扬（男声）",
  "zh-CN-liaoning-XiaobeiNeural": "晓北（东北女声）",
  "zh-CN-shaanxi-XiaoniNeural": "晓妮（陕西女声）",
};

export function voiceLabel(voiceId: string): string {
  const key = voiceId.trim();
  if (!key) return "—";
  return VOICE_LABELS[key] ?? key;
}

/** 列表性格列只露一行；完整内容在详情里看，不在表格里铺满。 */
export function personalityPreview(personality: string): string {
  const text = personality.trim();
  return text || "—";
}
