import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
export function money(v: unknown) {
  return v == null
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
      }).format(Number(v));
}
export function date(v: unknown) {
  return typeof v === "string" ? new Date(v).toLocaleString() : "—";
}
