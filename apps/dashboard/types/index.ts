export type Row = Record<string, unknown> & { id: string };
export type Page<T = Row> = {
  items: T[];
  total: number;
  page: number;
  page_size: number;
};
export type User = {
  id: string;
  username: string;
  role: "admin" | "operator" | "viewer";
  csrf_token: string;
};
export type Section =
  | "dashboard"
  | "accounts"
  | "authentication"
  | "nodes"
  | "sessions"
  | "scheduler"
  | "drops"
  | "inventory"
  | "market"
  | "analytics"
  | "logs"
  | "settings"
  | "protection";
export type Field = {
  name: string;
  label: string;
  type?:
    | "text"
    | "password"
    | "number"
    | "datetime-local"
    | "select"
    | "textarea"
    | "checkbox";
  required?: boolean;
  value?: string | number | boolean;
  options?: { label: string; value: string }[];
  min?: number;
  step?: string;
  hint?: string;
};
