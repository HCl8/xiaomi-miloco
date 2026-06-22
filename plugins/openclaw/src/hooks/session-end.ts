import { evictCatalogSession } from "../services/catalog.js";
import type { HookRegister } from "./index.js";

export const registerSessionEndHook: HookRegister = (api) => {
  api.on("session_end", (event) => {
    if (event.sessionKey) {
      void evictCatalogSession(event.sessionKey);
    }
  });
};
