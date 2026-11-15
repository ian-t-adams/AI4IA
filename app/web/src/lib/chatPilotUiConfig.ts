import { parseEnabledFlag } from "./envFlags";

export interface ChatPilotUiConfig {
  enabled: boolean;
}

export function getChatPilotUiConfig(): ChatPilotUiConfig {
  return { enabled: parseEnabledFlag(process.env.CHAT_PILOT_UI_ENABLED) };
}
