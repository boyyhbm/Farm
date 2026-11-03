"use client";
import { useState } from "react";
import {
  Search,
  ChevronLeft,
  ChevronRight,
  Inbox,
  RefreshCw,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Page, Row } from "@/types";
export type Column = {
  key: string;
  label: string;
  render?: (row: Row) => React.ReactNode;
};
export function Status({ value }: { value: unknown }) {
  const text = String(value ?? "UNKNOWN");
  const positive = [
    "ONLINE",
    "RUNNING",
    "CONFIRMED",
    "COMPLETED",
    "SUCCEEDED",
    "confirmed",
  ].includes(text.toUpperCase());
  const negative = [
    "FAILED",
    "OFFLINE",
    "TIMED_OUT",
    "REVOKED",
    "CANCELLED",
  ].includes(text.toUpperCase());
  return (
    <span
      className={`status ${positive ? "status-positive" : negative ? "status-negative" : ""}`}
    >
      <i />
      {text}
    </span>
  );
}
export function DataTable({
  data,
  columns,
  loading,
  error,
  page,
  onPage,
  search,
  onSearch,
  actions,
  selected,
  onSelect,
  empty = "No records yet",
}: {
  data?: Page;
  columns: Column[];
  loading?: boolean;
  error?: Error | null;
  page: number;
  onPage?: (page: number) => void;
  search?: string;
  onSearch?: (v: string) => void;
  actions?: React.ReactNode;
  selected?: string[];
  onSelect?: (ids: string[]) => void;
  empty?: string;
}) {
  const [local, setLocal] = useState(search || "");
  return (
    <section className="panel">
      <div className="table-toolbar">
        {onSearch && (
          <form
            className="search-field"
            onSubmit={(e) => {
              e.preventDefault();
              onSearch(local);
              onPage?.(1);
            }}
          >
            <Search size={16} />
            <input
              aria-label="Search"
              value={local}
              onChange={(e) => setLocal(e.target.value)}
              placeholder="Search / Поиск"
            />
            <button className="search-submit" type="submit">
              ↵
            </button>
          </form>
        )}
        <div className="toolbar-actions">{actions}</div>
      </div>
      {error ? (
        <div role="alert" className="error-banner">
          {error.message}
        </div>
      ) : loading ? (
        <div className="empty-state">
          <RefreshCw className="spin" size={24} />
          <p>Loading / Загрузка…</p>
        </div>
      ) : !data?.items?.length ? (
        <div className="empty-state">
          <Inbox size={34} />
          <p>{empty}</p>
        </div>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {onSelect && (
                  <th>
                    <input
                      type="checkbox"
                      aria-label="Select page"
                      checked={data.items.every((r) =>
                        selected?.includes(r.id),
                      )}
                      onChange={(e) =>
                        onSelect(
                          e.target.checked
                            ? Array.from(
                                new Set([
                                  ...(selected || []),
                                  ...data.items.map((r) => r.id),
                                ]),
                              )
                            : (selected || []).filter(
                                (id) => !data.items.some((r) => r.id === id),
                              ),
                        )
                      }
                    />
                  </th>
                )}
                {columns.map((c) => (
                  <th key={c.key}>{c.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => (
                <tr key={row.id}>
                  {onSelect && (
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Select ${row.id}`}
                        checked={selected?.includes(row.id) || false}
                        onChange={(e) =>
                          onSelect(
                            e.target.checked
                              ? [...(selected || []), row.id]
                              : (selected || []).filter((id) => id !== row.id),
                          )
                        }
                      />
                    </td>
                  )}
                  {columns.map((c) => (
                    <td key={c.key}>
                      {c.render ? c.render(row) : display(row[c.key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="table-footer">
        <span>
          {data?.total ?? 0} records · {page} /{" "}
          {Math.max(1, Math.ceil((data?.total || 0) / (data?.page_size || 50)))}
        </span>
        {onPage && (
          <div>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Previous page"
              disabled={page <= 1}
              onClick={() => onPage(page - 1)}
            >
              <ChevronLeft size={16} />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Next page"
              disabled={!data || page * data.page_size >= data.total}
              onClick={() => onPage(page + 1)}
            >
              <ChevronRight size={16} />
            </Button>
          </div>
        )}
      </div>
    </section>
  );
}
export function display(v: unknown): React.ReactNode {
  if (v == null || v === "") return <span className="muted">—</span>;
  if (typeof v === "object")
    return (
      <span className="mono truncate" title={JSON.stringify(v)}>
        {JSON.stringify(v)}
      </span>
    );
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return String(v);
}
