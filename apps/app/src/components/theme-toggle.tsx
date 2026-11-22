import { Monitor, Moon, Sun } from "lucide-react";

import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { isThemePreference, useTheme } from "@/lib/theme";

const LABELS = { light: "Light", dark: "Dark", system: "System" } as const;

/** Header control: light, dark or follow the system setting. */
export function ThemeToggle({ tip = false }: { tip?: boolean }) {
  const preference = useTheme((s) => s.preference);
  const resolved = useTheme((s) => s.resolved);
  const setPreference = useTheme((s) => s.setPreference);
  const Icon = preference === "system" ? Monitor : resolved === "dark" ? Moon : Sun;

  return (
    <DropdownMenu>
      <Tip label={`Theme: ${LABELS[preference]}`} disabled={!tip}>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" className="size-11" aria-label={`Theme: ${LABELS[preference]}`}>
            <Icon aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
      </Tip>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup
          value={preference}
          onValueChange={(value) => {
            if (isThemePreference(value)) setPreference(value);
          }}
        >
          <DropdownMenuRadioItem value="light">
            <Sun aria-hidden="true" /> Light
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">
            <Moon aria-hidden="true" /> Dark
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="system">
            <Monitor aria-hidden="true" /> System
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
