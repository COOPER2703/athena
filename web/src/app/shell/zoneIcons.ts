import type { ComponentType } from "react";

import type { IconName } from "../routing";
import {
  ConversationIcon,
  DiagnosticIcon,
  PluginsIcon,
  SettingsIcon,
  type IconProps,
} from "../../shared/icons";

/** Table unique des icônes de zone : navigation et états vides partagent. */
export const ZONE_ICONS: Readonly<
  Record<IconName, ComponentType<IconProps>>
> = {
  conversation: ConversationIcon,
  settings: SettingsIcon,
  plugins: PluginsIcon,
  diagnostic: DiagnosticIcon,
};
