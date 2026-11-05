"use client";
import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  LockKeyhole,
  Upload,
  Download,
  KeyRound,
  RefreshCw,
} from "lucide-react";
import { api, download } from "@/services/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { DataTable, Status } from "@/components/data-table";
import { ActionDialog, type FormSpec } from "@/components/action-dialog";
import { date } from "@/lib/utils";
import type { Page, Row } from "@/types";

type State = {
  available: boolean;
  unlocked: boolean;
  reason?: string;
  idle_seconds_remaining?: number;
};
type Preview = {
  id: string;
  items: {
    row_id: string;
    login: string | null;
    steam_id: string | null;
    password: string | null;
    guard_status: string;
    issues: string[];
    duplicate: boolean;
    login_required: boolean;
  }[];
  total: number;
  page: number;
  page_size: number;
  invalid: number;
  missing_guards: number;
  formats: string[];
};
type Mapping = {
  row_id: string;
  login?: string;
  steam_id?: string;
  skip?: boolean;
};
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const errText: Record<string, string> = {
  VAULT_LOCKED: "Хранилище заблокировано. Разблокируйте его паролем панели.",
  WINDOWS_DPAPI_REQUIRED:
    "Хранилище доступно в Windows Desktop с защитой DPAPI.",
  LOCAL_WINDOWS_DESKTOP_REQUIRED:
    "Используйте Vault в локальном Windows Manager на компьютере владельца.",
  INVALID_PANEL_PASSWORD: "Неверный пароль панели.",
  VAULT_KEY_UNAVAILABLE:
    "Windows не смогла открыть ключ DPAPI. Используйте исходную учётную запись Windows.",
  VAULT_KEY_MISSING:
    "Ключ Vault отсутствует. Секреты не сброшены; восстановите исходный профиль Windows.",
  IDENTIFIER_MISMATCH: "Идентификаторы аккаунта и файла Guard не совпадают.",
  INVALID_ROWS_MUST_BE_SKIPPED:
    "Исправьте сопоставление или явно пропустите ошибочные записи.",
  LOGIN_REQUIRED: "Укажите логин для файла Guard либо пропустите запись.",
  ZIP_EXPANSION_LIMIT: "Превышен безопасный лимит распаковки ZIP.",
  ZIP_UNSAFE_PATH: "ZIP содержит недопустимый путь или символическую ссылку.",
  ENCRYPTED_MAFILE_UNSUPPORTED:
    "Зашифрованный maFile не поддерживается. Его защита не обходится.",
  CREDENTIALS_MISSING_ON_THIS_DEVICE:
    "На этом ПК нет зашифрованной записи. Нужен повторный импорт владельцем.",
  INVALID_GUARD_SECRET: "Неверная структура секретов Steam Guard.",
};
function readable(value: unknown, ru: boolean) {
  const text = message(value);
  return ru ? errText[text] || text : text;
}

export function VaultImportWizard({
  ru,
  nodes,
  groups,
  onClose,
  onComplete,
}: {
  ru: boolean;
  nodes: Row[];
  groups: Row[];
  onClose: () => void;
  onComplete: () => void;
}) {
  const t = (r: string, e: string) => (ru ? r : e);
  const [id, setId] = useState("");
  const [step, setStep] = useState(1);
  const [files, setFiles] = useState<File[]>([]);
  const [formats, setFormats] = useState<Record<number, string>>({});
  const [rights, setRights] = useState(false);
  const [confirmation, setConfirmation] = useState(false);
  const [replace, setReplace] = useState(false);
  const [skipInvalid, setSkipInvalid] = useState(false);
  const [node, setNode] = useState("");
  const [group, setGroup] = useState("");
  const [page, setPage] = useState(1);
  const [mapping, setMapping] = useState<Record<string, Mapping>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const preview = useQuery({
    queryKey: ["vault-preview", id, page],
    queryFn: () =>
      api<Preview>(`/vault/imports/${id}/preview?page=${page}&page_size=25`),
    enabled: !!id && step >= 2,
    retry: false,
  });
  function kind(file: File, i: number) {
    return formats[i] || file.name.split(".").pop()?.toLowerCase() || "csv";
  }
  async function upload() {
    setBusy(true);
    setError("");
    let current = id;
    try {
      if (current) await api(`/vault/imports/${current}`, { method: "DELETE" });
      current = (
        await api<{ id: string }>("/vault/imports", {
          method: "POST",
          body: JSON.stringify({ confirmed: rights }),
        })
      ).id;
      setId(current);
      setMapping({});
      setPage(1);
      for (let i = 0; i < files.length; i++)
        await api(`/vault/imports/${current}/files?kind=${kind(files[i], i)}`, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: files[i],
        });
      setFiles([]);
      if (input.current) input.current.value = "";
      setStep(2);
    } catch (cause) {
      setError(readable(cause, ru));
    } finally {
      setBusy(false);
    }
  }
  async function commit() {
    setBusy(true);
    setError("");
    try {
      await api(`/vault/imports/${id}/commit`, {
        method: "POST",
        body: JSON.stringify({
          confirmed: confirmation,
          mappings: Object.values(mapping),
          replace_existing: replace,
          skip_invalid: skipInvalid,
          node_id: node || null,
          group_id: group || null,
        }),
      });
      onComplete();
      onClose();
    } catch (cause) {
      setError(readable(cause, ru));
    } finally {
      setBusy(false);
    }
  }
  async function close() {
    if (busy) return;
    if (id) {
      try {
        await api(`/vault/imports/${id}`, { method: "DELETE" });
      } catch {
        /* Locked previews expire and cannot be read. */
      }
    }
    onClose();
  }
  function edit(
    row: Preview["items"][number],
    key: "login" | "steam_id" | "skip",
    value: string | boolean,
  ) {
    setMapping((previous) => ({
      ...previous,
      [row.row_id]: {
        ...previous[row.row_id],
        row_id: row.row_id,
        [key]: value,
      },
    }));
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) void close();
      }}
    >
      <DialogContent
        title={t("Импорт аккаунтов", "Import Accounts")}
        description={t(
          "Секреты передаются только локальному Vault и не возвращаются в предварительный просмотр.",
          "Files are sent only to the local vault; secrets are never returned in the preview.",
        )}
      >
        <p className="muted">
          {t("Шаг", "Step")} {step}/3 ·{" "}
          {step === 1
            ? t("Файлы и формат", "Files and format")
            : step === 2
              ? t("Проверка и сопоставление", "Validation and mapping")
              : t("Подтверждение", "Confirmation")}
        </p>
        {step === 1 && (
          <div className="form-fields">
            <label className="field">
              <span>
                {t(
                  "Выберите CSV / TXT / JSON / maFile / ZIP",
                  "Select CSV / TXT / JSON / maFile / ZIP",
                )}
              </span>
              <input
                ref={input}
                type="file"
                  disabled={busy}
                multiple
                accept=".csv,.txt,.json,.maFile,.mafile,.zip"
                onChange={(e) => {
                  setFiles(Array.from(e.target.files || []));
                  setFormats({});
                }}
              />
            </label>
            {files.map((file, i) => (
              <label className="field" key={`${i}-${file.name}`}>
                <span>
                  {file.name} · {Math.ceil(file.size / 1024)} KB
                </span>
                <select
                  value={kind(file, i)}
                  onChange={(e) =>
                    setFormats({ ...formats, [i]: e.target.value })
                  }
                >
                  {["csv", "txt", "json", "mafile", "zip"].map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
            ))}
            <p className="muted">
              CSV: <code>login,password,steam_id</code> (
              {t("SteamID необязателен", "SteamID optional")}). TXT:{" "}
              <code>login:password</code>{" "}
              {t(
                "или логин, пароль и SteamID через Tab",
                "or tab-separated login, password, optional SteamID",
              )}
              . JSON:{" "}
              <code>
                {'{"accounts":[{"login":"…","password":"…","steam_id":"…"}]}'}
              </code>
              . ZIP:{" "}
              {t(
                "только maFile; до 5000 записей, 32 MiB ZIP / 64 MiB после распаковки",
                "maFiles only; 5000 records, 32 MiB ZIP / 64 MiB expanded",
              )}
              .{" "}
              {t(
                "Исходные файлы на вашем диске не удаляются.",
                "Original files on your disk are not deleted.",
              )}
            </p>
            <label className="check">
              <input
                type="checkbox"
                checked={rights}
                onChange={(e) => setRights(e.target.checked)}
              />
              {t(
                "Я вправе управлять всеми импортируемыми аккаунтами",
                "I am authorized to manage every imported account",
              )}
            </label>
            <Button
              disabled={!files.length || !rights || busy || files.length > 100}
              onClick={() => void upload()}
            >
              {busy
                ? t("Проверка…", "Validating…")
                : t("Проверить файлы", "Validate files")}
            </Button>
          </div>
        )}
        {step === 2 && (
          <>
            {preview.isPending && (
              <p role="status">
                {t("Подготовка просмотра…", "Loading preview…")}
              </p>
            )}
            {preview.error && (
              <p className="error-banner">{readable(preview.error, ru)}</p>
            )}
            {preview.data && (
              <>
                <p>
                  {preview.data.total} {t("записей", "records")} ·{" "}
                  {preview.data.invalid} {t("ошибочных", "invalid")} ·{" "}
                  {preview.data.missing_guards}{" "}
                  {t("без Guard", "without Guard")} ·{" "}
                  {preview.data.formats.join(", ")}
                </p>
                <div className="table-scroll vault-preview">
                  <table>
                    <thead>
                      <tr>
                        <th>{t("Пропустить", "Skip")}</th>
                        <th>Login</th>
                        <th>SteamID64</th>
                        <th>{t("Пароль / Guard", "Password / Guard")}</th>
                        <th>{t("Результат", "Result")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.data.items.map((row) => (
                        <tr key={row.row_id}>
                          <td>
                            <input
                              aria-label={`Skip ${row.row_id}`}
                              type="checkbox"
                              checked={mapping[row.row_id]?.skip || false}
                              onChange={(e) =>
                                edit(row, "skip", e.target.checked)
                              }
                            />
                          </td>
                          <td>
                            {row.login || (
                              <input
                                aria-label={`Login ${row.row_id}`}
                                maxLength={100}
                                value={mapping[row.row_id]?.login || ""}
                                onChange={(e) =>
                                  edit(row, "login", e.target.value)
                                }
                                placeholder="Login"
                              />
                            )}
                          </td>
                          <td>
                            {row.steam_id || (
                              <input
                                aria-label={`SteamID ${row.row_id}`}
                                inputMode="numeric"
                                maxLength={17}
                                value={mapping[row.row_id]?.steam_id || ""}
                                onChange={(e) =>
                                  edit(row, "steam_id", e.target.value)
                                }
                                placeholder={t(
                                  "Неизвестен — можно позже",
                                  "Unknown — link later",
                                )}
                              />
                            )}
                          </td>
                          <td>
                            {row.password || "—"} / {row.guard_status}
                          </td>
                          <td>
                            {row.issues.length
                              ? row.issues
                                  .map((code) => readable(code, ru))
                                  .join("; ")
                              : row.duplicate
                                ? t(
                                    "Дубликат: пропуск или явная замена",
                                    "Duplicate: skip or explicit replacement",
                                  )
                                : t("Готово", "Ready")}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="dialog-actions">
                  <Button
                    variant="secondary"
                    disabled={page <= 1}
                    onClick={() => setPage(page - 1)}
                  >
                    ←
                  </Button>
                  <span>
                    {page}/{Math.max(1, Math.ceil(preview.data.total / 25))}
                  </span>
                  <Button
                    variant="secondary"
                    disabled={page * 25 >= preview.data.total}
                    onClick={() => setPage(page + 1)}
                  >
                    →
                  </Button>
                </div>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={skipInvalid}
                    onChange={(e) => setSkipInvalid(e.target.checked)}
                  />
                  {t(
                    "Пропустить все записи с ошибками структуры",
                    "Skip all structurally invalid records",
                  )}
                </label>
                <Button
                  onClick={() => setStep(3)}
                  disabled={!preview.data.total}
                >
                  {t("К подтверждению", "Review import")}
                </Button>
              </>
            )}
          </>
        )}
        {step === 3 && (
          <div className="form-fields">
            <p>
              {t(
                "Записи без SteamID сохранятся в Vault без выдуманного профиля. Связать их можно позже. Авторизация выполняется вручную в Steam.",
                "Records without SteamID stay unlinked in the vault. Link them later; authentication remains manual in Steam.",
              )}
            </p>
            <label className="field">
              <span>
                {t(
                  "Назначить компьютер связанным аккаунтам",
                  "Assign node to linked accounts",
                )}
              </span>
              <select value={node} onChange={(e) => setNode(e.target.value)}>
                <option value="">—</option>
                {nodes
                  .filter((n) => !n.revoked)
                  .map((n) => (
                    <option value={n.id} key={n.id}>
                      {String(n.hostname)}
                    </option>
                  ))}
              </select>
            </label>
            <label className="field">
              <span>{t("Группа", "Group")}</span>
              <select value={group} onChange={(e) => setGroup(e.target.value)}>
                <option value="">—</option>
                {groups.map((g) => (
                  <option value={g.id} key={g.id}>
                    {String(g.name)}
                  </option>
                ))}
              </select>
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={replace}
                onChange={(e) => setReplace(e.target.checked)}
              />
              {t(
                "Разрешаю дополнить или заменить имеющиеся пароли и Guard для совпавших аккаунтов",
                "Allow supplementing or replacing credentials for matching existing accounts",
              )}
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={confirmation}
                onChange={(e) => setConfirmation(e.target.checked)}
              />
              {t(
                "Подтверждаю сопоставление и сохранение на этом ПК",
                "I confirm the mapping and storage on this PC",
              )}
            </label>
            <div className="dialog-actions">
              <Button
                variant="secondary"
                disabled={busy}
                onClick={() => setStep(2)}
              >
                {t("Назад", "Back")}
              </Button>
              <Button
                disabled={!confirmation || busy}
                onClick={() => void commit()}
              >
                {busy
                  ? t("Сохранение…", "Saving…")
                  : t("Начать импорт", "Start import")}
              </Button>
            </div>
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            {error}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function AccountVault({
  ru,
  role,
  nodes,
  groups,
  onInventory,
}: {
  ru: boolean;
  role: string;
  nodes: Row[];
  groups: Row[];
  onInventory: (id: string) => void;
}) {
  const t = (r: string, e: string) => (ru ? r : e);
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1),
    [q, setQ] = useState("");
  const [archived, setArchived] = useState(false),
    [selection, setSelection] = useState<string[]>([]);
  const [wizard, setWizard] = useState(false),
    [spec, setSpec] = useState<FormSpec | null>(null);
  const [notice, setNotice] = useState(""),
    [error, setError] = useState("");
  const admin = role === "admin";
  const state = useQuery({
    queryKey: ["vault-status"],
    queryFn: () => api<State>("/vault/status"),
    enabled: admin,
    refetchInterval: 5000,
    retry: false,
  });
  const ready = state.data?.available === true,
    unlocked = state.data?.unlocked === true;
  const records = useQuery({
    queryKey: ["vault-records", page, q, archived],
    queryFn: () =>
      api<Page>(
        `/vault/records?page=${page}&page_size=25&q=${encodeURIComponent(q)}&archived=${archived}`,
      ),
    enabled: admin && ready,
    refetchInterval: 10000,
    retry: false,
  });
  const jobs = useQuery({
    queryKey: ["vault-imports"],
    queryFn: () =>
      api<{
        items: (Row & {
          status: string;
          total: number;
          processed: number;
          imported: number;
          skipped: number;
          errors: { code: string; row_id?: string }[];
        })[];
      }>("/vault/imports"),
    enabled: admin && ready,
    refetchInterval: 2000,
    retry: false,
  });
  function refresh() {
    void queryClient.invalidateQueries({
      predicate: (query) => query.queryKey[0] !== "me",
    });
  }
  async function action(fn: () => Promise<unknown>) {
    setError("");
    try {
      await fn();
      refresh();
    } catch (cause) {
      setError(readable(cause, ru));
    }
  }
  function unlock() {
    setSpec({
      title: t("Разблокировать Vault", "Unlock vault"),
      description: t(
        "Введите пароль панели, а не пароль Steam. Блокировка: 5 минут без операций, максимум 15 минут.",
        "Enter your panel password, not a Steam password. Locks after 5 minutes idle, maximum 15 minutes.",
      ),
      fields: [
        {
          name: "password",
          label: t("Пароль панели", "Panel password"),
          type: "password",
          required: true,
        },
      ],
      submit: async (values) => {
        await api("/vault/unlock", {
          method: "POST",
          body: JSON.stringify({ password: values.password }),
        });
        refresh();
      },
    });
  }
  function bulk(operation: string) {
    setSpec({
      title: t("Подтвердите массовое действие", "Confirm bulk action"),
      description: `${selection.length} · ${operation}`,
      destructive: operation === "remove",
      fields:
        operation === "assign_node" || operation === "group"
          ? [
              {
                name: "value",
                label: t("Назначение", "Assignment"),
                type: "select",
                options: (operation === "assign_node"
                  ? nodes.filter((n) => !n.revoked)
                  : groups
                ).map((row) => ({
                  value: row.id,
                  label: String(row.hostname || row.name),
                })),
              },
            ]
          : [],
      submit: async (values) => {
        const result = await api<{ results: { id: string; code: string }[] }>(
          "/vault/records/bulk",
          {
            method: "POST",
            body: JSON.stringify({
              confirmed: true,
              ids: selection,
              operation,
              value: values.value || null,
            }),
          },
        );
        setNotice(result.results.map((r) => `${r.id}: ${r.code}`).join("; "));
        setSelection([]);
        refresh();
      },
    });
  }
  function manage(row: Row, operation: "link" | "password" | "guard" | "auth") {
    if (operation === "auth") {
      setSpec({
        title: t(
          "Подтверждение входа владельцем",
          "Owner sign-in confirmation",
        ),
        description: t(
          "Проверьте SteamID в официальном клиенте на выбранном ПК. Это ваше подтверждение, не проверка скрытой сессии Steam.",
          "Verify SteamID in the official client on the selected node. This records owner confirmation, not an inspection of Steam's private session.",
        ),
        fields: [
          {
            name: "node_id",
            label: t("Компьютер", "Node"),
            type: "select",
            required: true,
            value: String(row.assigned_node_id || ""),
            options: nodes
              .filter((n) => !n.revoked)
              .map((n) => ({ value: n.id, label: String(n.hostname) })),
          },
          {
            name: "status",
            label: t("Результат", "Result"),
            type: "select",
            required: true,
            value: "UNKNOWN",
            options: [
              {
                value: "AUTHORIZED",
                label: t(
                  "Вход и SteamID проверены",
                  "Sign-in and SteamID verified",
                ),
              },
              { value: "FAILED", label: t("Ошибка входа", "Sign-in failed") },
              { value: "UNKNOWN", label: t("Не проверено", "Not verified") },
            ],
          },
        ],
        submit: async (values) => {
          await api(`/vault/records/${row.id}/authentication`, {
            method: "POST",
            body: JSON.stringify({
              confirmed: true,
              node_id: values.node_id,
              status: values.status,
              error_code:
                values.status === "FAILED"
                  ? "OWNER_CONFIRMATION_FAILED"
                  : "NONE",
            }),
          });
          refresh();
        },
      });
    } else
      setSpec({
        title:
          operation === "link"
            ? t("Привязать SteamID64", "Link SteamID64")
            : t("Удалить секрет из Vault", "Remove secret from vault"),
        description: String(row.login),
        destructive: operation !== "link",
        fields:
          operation === "link"
            ? [{ name: "steam_id", label: "SteamID64", required: true }]
            : [],
        submit: async (values) => {
          await api(
            `/vault/records/${row.id}/${operation === "link" ? "link" : "remove-part"}`,
            {
              method: "POST",
              body: JSON.stringify({
                confirmed: true,
                ...(operation === "link"
                  ? { steam_id: values.steam_id }
                  : { part: operation }),
              }),
            },
          );
          refresh();
        },
      });
  }
  if (!admin)
    return (
      <section className="panel">
        <p>
          {t(
            "Vault доступен только администратору-владельцу.",
            "Vault access requires the owner administrator.",
          )}
        </p>
      </section>
    );
  return (
    <>
      <section className="panel vault-intro">
        <div className="section-title">
          <div>
            <h2>
              <KeyRound size={20} /> Steam Account Vault
            </h2>
            <p>
              {t(
                "Пароли и Guard остаются на ПК владельца. Проверка Vault подтверждает структуру и целостность записи, а не успешный вход в Steam. Секреты не раскрываются и не экспортируются.",
                "Passwords and Guard remain on the owner's PC. Vault validation checks record structure and integrity, not successful Steam sign-in. Secrets are not revealed or exported.",
              )}
            </p>
          </div>
          <Status value={unlocked ? "UNLOCKED" : "LOCKED"} />
        </div>
        <p className="muted">
          AES-256-GCM · Windows DPAPI ·{" "}
          {t(
            "Обычная резервная копия БД не содержит секретов Vault. Сохраните исходные файлы отдельно в защищённом месте.",
            "The standard database backup excludes vault secrets. Keep original source files separately in a secure location.",
          )}
        </p>
        {state.isPending && (
          <p role="status">{t("Проверка Vault…", "Checking vault…")}</p>
        )}
        {!ready && state.data && (
          <p className="error-banner">
            {readable(state.data.reason || "VAULT_UNAVAILABLE", ru)}
          </p>
        )}
        {state.error && (
          <p className="error-banner">{readable(state.error, ru)}</p>
        )}
        {ready && (
          <div className="toolbar-actions">
            <Button
              variant="secondary"
              onClick={() =>
                unlocked
                  ? void action(() => api("/vault/lock", { method: "POST" }))
                  : unlock()
              }
            >
              <LockKeyhole size={16} />
              {unlocked
                ? t("Заблокировать", "Lock")
                : t("Разблокировать", "Unlock")}
            </Button>
            <Button disabled={!unlocked} onClick={() => setWizard(true)}>
              <Upload size={16} />
              {t("Импорт аккаунтов", "Import Accounts")}
            </Button>
            <small>
              {unlocked
                ? `${state.data?.idle_seconds_remaining || 0}s`
                : t(
                    "Для операций нужен пароль панели",
                    "Panel password required for operations",
                  )}
            </small>
          </div>
        )}
      </section>
      {ready && (
        <>
          <div className="section-info">
            <p>
              {t(
                "Вход выполняется владельцем в официальном Steam с обычным Steam Guard. Сохранённая клиентом сессия Steam не сбрасывается при перезапуске Manager. Подтверждения привязаны к узлу и имеют срок.",
                "Sign in manually using official Steam and normal Steam Guard. Manager restart does not reset Steam's own saved session. Owner confirmations are node-bound and expire.",
              )}
            </p>
            <label className="check">
              <input
                type="checkbox"
                checked={archived}
                onChange={(e) => {
                  setArchived(e.target.checked);
                  setPage(1);
                  setSelection([]);
                }}
              />
              {t("Архив", "Archived")}
            </label>
          </div>
          <DataTable
            data={records.data}
            loading={records.isPending}
            error={records.error}
            page={page}
            onPage={setPage}
            search={q}
            onSearch={(v) => {
              setQ(v);
              setPage(1);
            }}
            selected={selection}
            onSelect={setSelection}
            empty={t(
              "Импортируйте собственные аккаунты",
              "Import your own accounts",
            )}
            actions={
              <>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    void action(() =>
                      download("/vault/export", "vault-metadata.csv"),
                    )
                  }
                >
                  <Download size={14} />
                  {t("Метаданные", "Metadata")}
                </Button>
                {[
                  ["validate", t("Проверить", "Validate")],
                  ["assign_node", t("Компьютер", "Assign Node")],
                  ["group", t("Группа", "Group")],
                  ["archive", t("Архив", "Archive")],
                  [
                    "inventory_sync",
                    t("Обновить инвентарь", "Refresh inventory"),
                  ],
                  ["remove", t("Удалить данные", "Remove credentials")],
                ].map(([op, label]) => (
                  <Button
                    key={op}
                    size="sm"
                    variant="secondary"
                    disabled={
                      !unlocked || !selection.length || selection.length > 1000
                    }
                    onClick={() => bulk(op)}
                  >
                    {label}
                  </Button>
                ))}
              </>
            }
            columns={[
              {
                key: "login",
                label: "Login",
                render: (row) => (
                  <>
                    <strong>{String(row.login)}</strong>
                    <small className="mono">{row.id}</small>
                  </>
                ),
              },
              {
                key: "steam_id",
                label: "SteamID64 / Account ID",
                render: (row) => (
                  <>
                    {String(row.steam_id || "Unknown")}
                    <small className="mono">
                      {String(
                        row.account_id ||
                          t("Требуется привязка", "Link required"),
                      )}
                    </small>
                  </>
                ),
              },
              {
                key: "credential_status",
                label: t("Учётные данные", "Credentials"),
                render: (row) => (
                  <>
                    <Status value={row.credential_status} />
                    <small>
                      {t("Пароль", "Password")}:{" "}
                      {row.password_present ? "••••••" : "—"} · Guard:{" "}
                      {String(row.guard_status)}
                    </small>
                  </>
                ),
              },
              {
                key: "assigned_node_id",
                label: t("Узел / сессия", "Node / session"),
                render: (row) => (
                  <>
                    {String(
                      nodes.find((n) => n.id === row.assigned_node_id)
                        ?.hostname || "—",
                    )}
                    <small>
                      {String(row.session_status)} ·{" "}
                      {String(row.authentication_status)}
                    </small>
                  </>
                ),
              },
              {
                key: "last_login_attempt",
                label: t("Последняя попытка", "Last attempt"),
                render: (row) => (
                  <>
                    {date(row.last_login_attempt)}
                    <small>
                      {String(row.last_authentication_error || "—")}
                    </small>
                  </>
                ),
              },
              {
                key: "actions",
                label: t("Действия", "Actions"),
                render: (row) => (
                  <div className="vault-row-actions">
                    {!row.account_id && (
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={!unlocked}
                        onClick={() => manage(row, "link")}
                      >
                        SteamID
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={!unlocked}
                      onClick={() =>
                        void action(async () => {
                          await api(
                            `/vault/records/${row.id}/authentication/start`,
                            {
                              method: "POST",
                              body: JSON.stringify({ confirmed: true }),
                            },
                          );
                          if (window.farmDesktop?.openSteam)
                            await window.farmDesktop.openSteam();
                          setNotice(
                            t(
                              "Откройте официальный Steam на нужном ПК, войдите вручную и проверьте SteamID. Пароль из Vault не подставляется.",
                              "Open official Steam on the correct PC, sign in manually and verify SteamID. Vault credentials are not filled in.",
                            ),
                          );
                        })
                      }
                    >
                      {t("Ручной вход", "Manual sign-in")}
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={!unlocked || !row.account_id}
                      onClick={() => manage(row, "auth")}
                    >
                      {t("Подтвердить вход", "Confirm sign-in")}
                    </Button>
                    {!!row.password_present && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={!unlocked}
                        onClick={() => manage(row, "password")}
                      >
                        {t("Удалить пароль", "Remove password")}
                      </Button>
                    )}
                    {row.guard_status === "LINKED" && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={!unlocked}
                        onClick={() => manage(row, "guard")}
                      >
                        {t("Удалить Guard", "Remove Guard")}
                      </Button>
                    )}
                    {!!row.account_id && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onInventory(String(row.account_id))}
                      >
                        {t("Инвентарь", "Inventory")}
                      </Button>
                    )}
                  </div>
                ),
              },
            ]}
          />
          <section className="panel">
            <h3>
              <RefreshCw size={16} />
              {t("Фоновые импорты", "Background imports")}
            </h3>
            {jobs.error && (
              <p className="error-banner">{readable(jobs.error, ru)}</p>
            )}
            {jobs.data?.items.length === 0 && (
              <p className="muted">
                {t("Импортов пока нет", "No imports yet")}
              </p>
            )}
            {jobs.data?.items.map((job) => (
              <div className="vault-job" key={job.id}>
                <Status value={job.status} />
                <span>
                  {job.processed}/{job.total} · {t("сохранено", "stored")}:{" "}
                  {job.imported} · {t("пропущено", "skipped")}: {job.skipped} ·{" "}
                  {t("ошибок", "errors")}: {job.errors.length}
                </span>
                {["FAILED", "WAITING_FOR_UNLOCK"].includes(job.status) && (
                  <Button
                    size="sm"
                    disabled={!unlocked}
                    onClick={() =>
                      setSpec({
                        title: t("Возобновить импорт", "Resume import"),
                        description: String(job.id),
                        fields: [],
                        submit: async () => {
                          await api(`/vault/imports/${job.id}/resume`, {
                            method: "POST",
                            body: JSON.stringify({ confirmed: true }),
                          });
                          refresh();
                        },
                      })
                    }
                  >
                    {t("Возобновить", "Resume")}
                  </Button>
                )}
                {["QUEUED", "RUNNING", "WAITING_FOR_UNLOCK", "FAILED"].includes(
                  job.status,
                ) && (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={!unlocked}
                    onClick={() =>
                      setSpec({
                        title: t(
                          "Отменить оставшийся импорт",
                          "Cancel remaining import",
                        ),
                        description: t(
                          "Уже сохранённые записи останутся в Vault.",
                          "Already saved records remain in the vault.",
                        ),
                        fields: [],
                        submit: async () => {
                          await api(`/vault/imports/${job.id}`, {
                            method: "DELETE",
                          });
                          refresh();
                        },
                      })
                    }
                  >
                    {t("Отменить", "Cancel")}
                  </Button>
                )}
                {job.errors.length > 0 && (
                  <details>
                    <summary>{t("Диагностика", "Diagnostics")}</summary>
                    {job.errors.slice(0, 100).map((entry, i) => (
                      <p key={i}>
                        {entry.row_id || "—"}: {readable(entry.code, ru)}
                      </p>
                    ))}
                  </details>
                )}
              </div>
            ))}
          </section>
        </>
      )}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="error-banner">
          {error}
        </p>
      )}
      <ActionDialog
        spec={spec}
        onClose={() => setSpec(null)}
        onSuccess={() => refresh()}
      />
      {wizard && (
        <VaultImportWizard
          ru={ru}
          nodes={nodes}
          groups={groups}
          onClose={() => setWizard(false)}
          onComplete={() => {
            setNotice(
              t(
                "Импорт поставлен в локальную очередь. Следите за фактическим результатом ниже.",
                "Import queued locally. Follow its actual result below.",
              ),
            );
            refresh();
          }}
        />
      )}
    </>
  );
}
