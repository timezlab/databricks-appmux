"use client";

import type { LucideIcon } from "lucide-react";
import { MonitorIcon, MoonIcon, SunIcon } from "lucide-react";

import { THEME_CHOICES, useThemeChoice } from "@/hooks/use-theme-choice";
import type { ThemeChoice } from "@/hooks/use-theme-choice";
import { Button as UiButton } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

const THEME_LABEL: Readonly<Record<ThemeChoice, string>> = {
  light: "Light",
  dark: "Dark",
  system: "System",
};

const THEME_ICON: Readonly<Record<ThemeChoice, LucideIcon>> = {
  light: SunIcon,
  dark: MoonIcon,
  system: MonitorIcon,
};

function isThemeChoice(value: string): value is ThemeChoice {
  return (THEME_CHOICES as readonly string[]).includes(value);
}

const TRIGGER_CLASSES = "text-muted-foreground hover:text-foreground";

/**
 * Light, Dark or System, as shadcn's theme button: a sun in light, a moon in
 * dark, and a menu of the three. The button's name says the current choice
 * ("Theme: System"). The rule lives in public/theme.js (ADR 0019); this only
 * calls it. Without that script (blocked) the page follows the OS through CSS
 * and the button says so instead of offering a choice it cannot apply.
 */
export function ThemeToggle() {
  const { choice, choose } = useThemeChoice();

  // The icon follows the theme on screen, not the choice: CSS draws it, so
  // System shows the OS's theme and changes with it.
  const icons = (
    <>
      <SunIcon className="dark:hidden" aria-hidden="true" />
      <MoonIcon className="hidden dark:block" aria-hidden="true" />
    </>
  );

  if (choice === null) {
    return (
      <UiButton
        type="button"
        variant="ghost"
        size="icon"
        className={TRIGGER_CLASSES}
        disabled
        aria-label="Theme: System (the theme switch did not load)"
        title="The theme switch did not load; the page follows the system theme."
      >
        {icons}
      </UiButton>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UiButton
          type="button"
          variant="ghost"
          size="icon"
          className={TRIGGER_CLASSES}
          aria-label={`Theme: ${THEME_LABEL[choice]}`}
          title={`Theme: ${THEME_LABEL[choice]}`}
        >
          {icons}
        </UiButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-36">
        <DropdownMenuLabel className="text-xs text-muted-foreground">
          Theme
        </DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={choice}
          onValueChange={(value) => {
            if (isThemeChoice(value)) choose(value);
          }}
        >
          {THEME_CHOICES.map((option) => {
            const Icon = THEME_ICON[option];
            return (
              <DropdownMenuRadioItem key={option} value={option}>
                <Icon className="text-muted-foreground" aria-hidden="true" />
                {THEME_LABEL[option]}
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
