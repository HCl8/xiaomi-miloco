import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { getPluginConfig, type MilocoPluginConfig } from "../config.js";
import { registerBeforePromptBuildHook } from "./prompt.js";
import { registerSessionEndHook } from "./session-end.js";
import { registerTraceHooks } from "./trace.js";

export type HookRegister = (
  api: OpenClawPluginApi,
  config: MilocoPluginConfig,
) => void;

const kRegisters: HookRegister[] = [
  registerBeforePromptBuildHook, // 系统提示词扩展
  registerSessionEndHook, // session 结束清理 catalog 缓存
  registerTraceHooks, // rule 全生命周期跟踪（MILOCO_TRACE=rule 启用）
];

export function registerHooks(api: OpenClawPluginApi) {
  const config = getPluginConfig(api);
  for (const register of kRegisters) {
    register(api, config);
  }
}
