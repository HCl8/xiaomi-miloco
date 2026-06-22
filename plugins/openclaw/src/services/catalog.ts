import { logger } from "../utils/logger.js";
import { readTextFileSync } from "../utils/io.js";
import { milocoConfigFile } from "../miloco/paths.js";

/**
 * Device catalog injection (spec-injection-plan §5.4).
 *
 * 调后端 ``GET /api/miot/catalog?session_key=xxx``。
 * 缓存在后端内存（24h TTL, max 20 entries, switch_home/refresh 清空）。
 *
 * - 传 sessionKey：走后端缓存，同 session 冻结不变 → Anthropic prompt cache 命中。
 * - 不传 sessionKey：每次全量生成，5s 节流防同 turn spam。
 *
 * 调后端失败时返回空字符串。
 */

interface ServerConfig {
  url: string;
  token: string;
}

let _serverConfig: ServerConfig | null = null;

function getServerConfig(): ServerConfig {
  if (_serverConfig) return _serverConfig;
  try {
    const raw = JSON.parse(readTextFileSync(milocoConfigFile()));
    _serverConfig = {
      url: (raw?.server?.url as string) || "http://127.0.0.1:1810",
      token: (raw?.server?.token as string) || "",
    };
    return _serverConfig;
  } catch {
    return { url: "http://127.0.0.1:1810", token: "" };
  }
}

async function fetchCatalog(sessionKey?: string): Promise<string> {
  const cfg = getServerConfig();
  const params = sessionKey
    ? `?session_key=${encodeURIComponent(sessionKey)}`
    : "";
  try {
    const resp = await fetch(`${cfg.url}/api/miot/catalog${params}`, {
      method: "GET",
      headers: { Authorization: `Bearer ${cfg.token}` },
      signal: AbortSignal.timeout(10_000),
    });
    if (!resp.ok) {
      logger.warn(`catalog fetch HTTP ${resp.status}`);
      return "";
    }
    const json = await resp.json();
    return json?.data?.catalog ?? "";
  } catch (err) {
    logger.warn(`catalog fetch failed: ${err}`);
    return "";
  }
}

const THROTTLE_MS = 5_000;
let _lastFetchAt = 0;
let _lastNoSessionResult = "";

export async function getCatalog(sessionKey?: string): Promise<string> {
  if (sessionKey) {
    return fetchCatalog(sessionKey);
  }
  // 无 sessionKey：5s 节流，返回上次结果
  const now = Date.now();
  if (now - _lastFetchAt < THROTTLE_MS) {
    return _lastNoSessionResult;
  }
  _lastFetchAt = now;
  const text = await fetchCatalog();
  _lastNoSessionResult = text;
  return text;
}

export async function evictCatalogSession(sessionKey: string): Promise<void> {
  const cfg = getServerConfig();
  try {
    await fetch(
      `${cfg.url}/api/miot/catalog/${encodeURIComponent(sessionKey)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${cfg.token}` },
        signal: AbortSignal.timeout(5_000),
      },
    );
  } catch {
    // best-effort, silent
  }
}

export function _resetCatalogCache(): void {
  _lastFetchAt = 0;
  _serverConfig = null;
}
