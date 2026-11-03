import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Section } from "@/types";
export const useUI = create(
  persist<{
    locale: "ru" | "en";
    setLocale: (v: "ru" | "en") => void;
    section: Section;
    setSection: (v: Section) => void;
  }>(
    (set) => ({
      locale: "ru",
      setLocale: (locale) => set({ locale }),
      section: "dashboard",
      setSection: (section) => set({ section }),
    }),
    { name: "farm-ui" },
  ),
);
