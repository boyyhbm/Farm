"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, RefreshCw, OctagonX, KeyRound } from "lucide-react";
import { api } from "@/services/api";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/data-table";
import { ActionDialog, type FormSpec } from "@/components/action-dialog";
import { date } from "@/lib/utils";
import type { Row, Page, Field } from "@/types";

type Risk = {
  level: string;
  reasons: { code: string; source: string; observed_at: string | null }[];
};
type Overview = {
  level: string;
  observed_at: string;
  accounts: number;
  restricted_accounts: number;
  verified_accounts: number;
  open_incidents: number;
  failed_panel_logins_24h: number;
  sync_failures_24h: number;
  desktop: boolean;
  provider: {
    configured: boolean;
    status: string;
    checked_at: string | null;
    retry_at: string | null;
    error: string | null;
    source: string;
  };
  policy: {
    require_fresh_checks: boolean;
    check_interval_seconds: number;
    max_check_age_seconds: number;
  };
  infrastructure_reasons: {
    code: string;
    node_id: string;
    source: string;
    observed_at: string | null;
  }[];
  nodes: (Row & {
    hostname: string;
    status: string;
    revoked: boolean;
    transport_security: string;
    transport_checked_at: string | null;
    last_heartbeat: string | null;
  })[];
};
function useOverview() {
  return useQuery({
    queryKey: ["protection-overview"],
    queryFn: () => api<Overview>("/protection/overview"),
    refetchInterval: 15000,
  });
}
function Badge({ level }: { level: unknown }) {
  return (
    <span className={`risk-badge risk-${String(level).toLowerCase()}`}>
      {String(level || "UNKNOWN")}
    </span>
  );
}
export function ProtectionSummary({
  ru,
  onOpen,
}: {
  ru: boolean;
  onOpen: () => void;
}) {
  const result = useOverview();
  return (
    <section className="panel protection-summary">
      <ShieldCheck size={20} />
      <strong>Protection</strong>
      <Badge level={result.data?.level} />
      <span>
        {result.error
          ? result.error.message
          : result.data
            ? `${ru ? "Открытых инцидентов" : "Open incidents"}: ${result.data.open_incidents}`
            : ru
              ? "Проверка…"
              : "Checking…"}
      </span>
      <Button variant="secondary" size="sm" onClick={onOpen}>
        {ru ? "Открыть" : "Open"}
      </Button>
    </section>
  );
}
export default function Protection({
  ru,
  role,
}: {
  ru: boolean;
  role: string;
}) {
  const t = (a: string, b: string) => (ru ? a : b);
  const canOperate = role === "admin" || role === "operator";
  const isAdmin = role === "admin";
  const client = useQueryClient();
  const overview = useOverview();
  const [page, setPage] = useState(1),
    [incidentPage, setIncidentPage] = useState(1),
    [search, setSearch] = useState("");
  const [level, setLevel] = useState("");
  const [selected, setSelected] = useState("");
  const [form, setForm] = useState<FormSpec | null>(null);
  const [notice, setNotice] = useState("");
  const accounts = useQuery({
    queryKey: ["protection-accounts", page, search, level],
    queryFn: () =>
      api<Page>(
        `/protection/accounts?page=${page}&page_size=25&q=${encodeURIComponent(search)}${level ? "&level=" + level : ""}`,
      ),
    refetchInterval: 15000,
  });
  const incidents = useQuery({
    queryKey: ["protection-incidents", incidentPage],
    queryFn: () =>
      api<Page>(`/protection/incidents?page=${incidentPage}&page_size=25`),
    refetchInterval: 15000,
  });
  const detail = useQuery({
    queryKey: ["protection-detail", selected],
    queryFn: () =>
      api<Row & { history: Row[] }>(`/protection/accounts/${selected}`),
    enabled: !!selected,
  });
  const confirm: Field = {
    name: "confirmed",
    label: t("Подтверждаю действие", "I confirm this operation"),
    type: "checkbox",
    required: true,
  };
  const note: Field = {
    name: "note",
    label: t(
      "Основание (от 5 символов, без секретов)",
      "Reason (5+ characters, no secrets)",
    ),
    type: "textarea",
    required: true,
  };
  const act = (
    title: string,
    description: string,
    path: string,
    body: Record<string, unknown> = {},
    destructive = false,
    fields: Field[] = [],
  ) =>
    setForm({
      title,
      description,
      fields,
      destructive,
      submit: (v) =>
        api(path, { method: "POST", body: JSON.stringify({ ...body, ...v }) }),
    });
  const bool = (value: unknown) =>
    value === null || value === undefined
      ? t("Неизвестно", "Unknown")
      : value
        ? t("Да", "Yes")
        : t("Нет при последней проверке", "No at last check");
  const authorize = (r: Row) =>
    act(
      t("Подтверждение входа в Steam", "Steam sign-in confirmation"),
      t(
        "Вручную проверьте SteamID в официальном клиенте на выбранном ПК. Это подтверждение оператора с ограниченным сроком, а не проверка входа через Steam API. Ошибка входа остановит затронутые задания.",
        "Manually verify SteamID in the official client on the selected PC. This is a time-limited operator attestation, not Steam API login verification. Reporting failed authorization stops affected jobs.",
      ),
      `/protection/accounts/${r.id}/authorization`,
      {},
      true,
      [
        {
          name: "status",
          label: t("Состояние", "Status"),
          type: "select",
          required: true,
          value: "AUTHORIZED",
          options: [
            {
              value: "AUTHORIZED",
              label: t("Вход вручную проверен", "Manually verified"),
            },
            {
              value: "FAILED",
              label: t("Обнаружена ошибка входа", "Authorization failed"),
            },
            { value: "UNKNOWN", label: t("Состояние неизвестно", "Unknown") },
          ],
        },
        {
          name: "node_id",
          label: t("Компьютер", "Node"),
          type: "select",
          required: true,
          value: String(r.auth_node_id || r.assigned_node_id || ""),
          options: overview.data?.nodes
            .filter((n) => !n.revoked)
            .map((n) => ({ value: n.id, label: n.hostname })),
        },
        {
          name: "expires_minutes",
          label: t(
            "Срок подтверждения, минут (5–720)",
            "Confirmation lifetime, minutes (5–720)",
          ),
          type: "number",
          value: 720,
          min: 5,
        },
        note,
        confirm,
      ],
    );
  return (
    <div className="protection-page">
      <div className="section-info">
        <p>
          {t(
            "Оценка проверяемых технических и операционных событий. Прогноз VAC-бана не выполняется.",
            "Assessment of observable technical and operational events. VAC bans are not predicted.",
          )}
        </p>
        <ShieldCheck size={23} />
      </div>
      {overview.error && (
        <div className="error-banner" role="alert">
          {overview.error.message}
        </div>
      )}
      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      <div className="protection-grid">
        {[
          [
            t("Общий уровень", "Overall risk"),
            <Badge key="level" level={overview.data?.level} />,
          ],
          [
            t("Аккаунты с ограничениями", "Accounts with restrictions"),
            overview.data?.restricted_accounts,
          ],
          [
            t("Открытые инциденты", "Open incidents"),
            overview.data?.open_incidents,
          ],
          [
            t("Ошибки входа в панель / 24ч", "Failed panel logins / 24h"),
            overview.data?.failed_panel_logins_24h,
          ],
          [
            t("Ошибки синхронизации / 24ч", "Sync failures / 24h"),
            overview.data?.sync_failures_24h,
          ],
        ].map(([label, value], i) => (
          <section className="panel" key={i}>
            <small>{label}</small>
            <h2>{value ?? "—"}</h2>
          </section>
        ))}
      </div>
      <section className="panel">
        <div className="protection-actions">
          <h3>Steam Web API</h3>
          <Badge level={overview.data?.provider.status} />
          {canOperate && (
            <Button
              variant="secondary"
              onClick={() =>
                act(
                  t("Проверить доступные сведения", "Check available evidence"),
                  t(
                    "Проверить до 100 аккаунтов через официальный GetPlayerBans. Фоновые проверки последовательно обходят весь реестр.",
                    "Check up to 100 accounts using official GetPlayerBans. Background batches rotate through the registry.",
                  ),
                  "/protection/check",
                  { account_ids: [] },
                )
              }
            >
              <RefreshCw size={14} />
              {t("Проверить сейчас", "Check now")}
            </Button>
          )}
          {isAdmin && overview.data?.desktop && (
            <Button
              variant="secondary"
              onClick={() =>
                act(
                  t("Ключ Steam Web API", "Steam Web API key"),
                  t(
                    "Ключ сохраняется через DPAPI на главном ПК. Пустое поле отключает интеграцию.",
                    "The key is stored with DPAPI on the main PC. An empty field disables this integration.",
                  ),
                  "/protection/key",
                  {},
                  false,
                  [
                    {
                      name: "api_key",
                      label: t(
                        "Ключ (32 hex символа)",
                        "Key (32 hexadecimal characters)",
                      ),
                      type: "password",
                    },
                  ],
                )
              }
            >
              <KeyRound size={14} />
              {t("Настроить ключ", "Configure key")}
            </Button>
          )}
          {isAdmin && overview.data?.provider.status === "AUTH_FAILED" && (
            <Button
              variant="secondary"
              onClick={() =>
                act(
                  t(
                    "Повторить после исправления ключа",
                    "Retry after fixing credentials",
                  ),
                  t(
                    "Проверьте настройки ключа. Ошибка Web API не означает блокировку аккаунтов.",
                    "Review API credentials. An API authentication failure does not mean accounts are banned.",
                  ),
                  "/protection/provider/retry",
                  { hold: false },
                  false,
                  [note, confirm],
                )
              }
            >
              {t("Разрешить повторную проверку", "Allow retry")}
            </Button>
          )}
          {canOperate && (
            <Button
              variant="destructive"
              onClick={() =>
                act(
                  t("Аварийная остановка", "Emergency stop"),
                  t(
                    "Остановить все управляемые сессии и приостановить очередь? Для offline-узлов команды сохраняются до восстановления связи.",
                    "Stop all managed sessions and pause the queue? Offline-node stop commands remain pending until connection recovery.",
                  ),
                  "/control/emergency-stop",
                  {},
                  true,
                )
              }
            >
              <OctagonX size={14} />
              {t("Аварийная остановка", "Emergency stop")}
            </Button>
          )}
        </div>
        <p>
          {overview.data?.provider.configured
            ? t("Ключ настроен", "Key configured")
            : t(
                "Ключ не настроен: статусы блокировок неизвестны.",
                "Key not configured: ban statuses are unknown.",
              )}{" "}
          · {t("Последняя попытка", "Last attempt")}:{" "}
          {date(overview.data?.provider.checked_at)} ·{" "}
          {t("Повтор не раньше", "Retry after")}:{" "}
          {date(overview.data?.provider.retry_at)}
        </p>
        {overview.data?.provider.error && (
          <p role="alert">{overview.data.provider.error}</p>
        )}
        <small>
          {overview.data?.provider.source} ·{" "}
          <a
            href="https://partner.steamgames.com/doc/webapi/ISteamUser#GetPlayerBans"
            target="_blank"
            rel="noreferrer"
          >
            Steam documentation
          </a>
        </small>
      </section>
      <div className="protection-actions">
        <h3>{t("Аккаунты и источники", "Accounts and evidence")}</h3>
        <label>
          {t("Уровень", "Level")}{" "}
          <select
            value={level}
            onChange={(e) => {
              setLevel(e.target.value);
              setPage(1);
            }}
          >
            <option value="">{t("Все", "All")}</option>
            {["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </label>
      </div>
      <DataTable
        data={accounts.data}
        loading={accounts.isPending}
        error={accounts.error}
        page={page}
        onPage={setPage}
        search={search}
        onSearch={(v) => {
          setSearch(v);
          setPage(1);
        }}
        columns={[
          { key: "steam_id", label: "SteamID64" },
          {
            key: "risk",
            label: t("Риск", "Risk"),
            render: (r) => <Badge level={(r.risk as Risk).level} />,
          },
          {
            key: "vac_banned",
            label: "VAC",
            render: (r) => bool(r.vac_banned),
          },
          {
            key: "community_banned",
            label: "Community",
            render: (r) => bool(r.community_banned),
          },
          { key: "game_bans", label: "Game bans" },
          { key: "economy_ban", label: "Economy" },
          { key: "check_status", label: t("Проверка", "Check") },
          {
            key: "checked_at",
            label: t("Источник / время", "Source / time"),
            render: (r) => (
              <span>
                {String(r.source || "—")}
                <small className="block">{date(r.checked_at)}</small>
              </span>
            ),
          },
          {
            key: "auth_status",
            label: t("Вход (оператор)", "Sign-in (operator)"),
            render: (r) => (
              <span>
                {String(r.auth_status)}
                <small className="block">
                  {String(r.auth_source || "—")} · {t("до", "until")}{" "}
                  {date(r.auth_expires_at)}
                </small>
              </span>
            ),
          },
          {
            key: "actions",
            label: "",
            render: (r) => (
              <div className="row-actions">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setSelected(r.id)}
                >
                  {t("История", "History")}
                </Button>
                {canOperate && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => authorize(r)}
                  >
                    {t("Вход", "Sign-in")}
                  </Button>
                )}
                {isAdmin && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      act(
                        t("Ручная приостановка", "Manual hold"),
                        t(
                          "Изменение ручной приостановки не снимает подтверждённые ограничения Steam.",
                          "Changing a manual hold does not clear confirmed Steam restrictions.",
                        ),
                        `/protection/accounts/${r.id}/hold`,
                        { hold: !r.manual_hold },
                        true,
                        [note, confirm],
                      )
                    }
                  >
                    {r.manual_hold
                      ? t("Снять паузу", "Release hold")
                      : t("Приостановить", "Hold")}
                  </Button>
                )}
                {canOperate && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      act(
                        t("Проверка аккаунта", "Account check"),
                        String(r.steam_id),
                        "/protection/check",
                        { account_ids: [r.id] },
                      )
                    }
                  >
                    <RefreshCw size={13} />
                  </Button>
                )}
              </div>
            ),
          },
        ]}
      />
      <p className="muted">
        {t(
          "Game bans относятся к аккаунту; конкретная игра может быть неизвестна. Ограничения торговли через GetPlayerBans не доступны. Дата показывает последнее успешное подтверждение, а не гарантию текущего состояния.",
          "Game bans are account-wide; the affected game may be unknown. GetPlayerBans does not expose account trade restrictions. The timestamp identifies the last successful observation, not a guarantee of current status.",
        )}
      </p>
      {selected && (
        <section className="panel">
          <div className="protection-actions">
            <h3>
              {t("Доказательства и история", "Evidence and history")}:{" "}
              {String(detail.data?.steam_id || selected)}
            </h3>
            <Button variant="ghost" onClick={() => setSelected("")}>
              {t("Закрыть", "Close")}
            </Button>
          </div>
          {detail.error ? (
            <p role="alert">{detail.error.message}</p>
          ) : (
            <>
              <p>
                {t("Причины оценки", "Risk reasons")}:{" "}
                {((detail.data?.risk as Risk | undefined)?.reasons || [])
                  .map((r) => `${r.code} (${r.source})`)
                  .join("; ") || "—"}
              </p>
              <p>
                {t("Авторизация подтверждена", "Authorization attested")}:{" "}
                {String(detail.data?.auth_source || "—")} ·{" "}
                {date(detail.data?.auth_checked_at)} · {t("до", "until")}{" "}
                {date(detail.data?.auth_expires_at)}
              </p>
              <pre className="protection-history">
                {detail.data
                  ? JSON.stringify(detail.data.history, null, 2)
                  : t("Загрузка…", "Loading…")}
              </pre>
            </>
          )}
        </section>
      )}
      <h3>{t("Журнал инцидентов", "Incident timeline")}</h3>
      <DataTable
        data={incidents.data}
        loading={incidents.isPending}
        error={incidents.error}
        page={incidentPage}
        onPage={setIncidentPage}
        columns={[
          {
            key: "last_seen",
            label: t("Последнее событие", "Last seen"),
            render: (r) => date(r.last_seen),
          },
          {
            key: "severity",
            label: t("Уровень", "Severity"),
            render: (r) => <Badge level={r.severity} />,
          },
          { key: "code", label: t("Событие", "Event") },
          { key: "status", label: t("Состояние", "State") },
          { key: "account_id", label: "Account ID" },
          { key: "node_id", label: "Node ID" },
          {
            key: "recommendation",
            label: t("Источник и действия", "Source and action"),
            render: (r) => (
              <span>
                {String(r.source)}
                <small className="block">{String(r.recommendation)}</small>
              </span>
            ),
          },
          { key: "occurrences", label: t("Событий", "Events") },
          {
            key: "actions",
            label: "",
            render: (r) =>
              r.status !== "RESOLVED" && canOperate ? (
                <div className="row-actions">
                  {r.status === "OPEN" && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        act(
                          t("Принять инцидент", "Acknowledge incident"),
                          String(r.recommendation),
                          `/protection/incidents/${r.id}/respond`,
                          { action: "acknowledge" },
                          false,
                          [note, confirm],
                        )
                      }
                    >
                      {t("Принять", "Acknowledge")}
                    </Button>
                  )}
                  {isAdmin && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        act(
                          t("Закрыть инцидент", "Resolve incident"),
                          t(
                            "Закрытие записи не снимает подтверждённую блокировку и не разрешает запрещённые операции.",
                            "Resolving the case does not clear ban evidence or permit prohibited operations.",
                          ),
                          `/protection/incidents/${r.id}/respond`,
                          { action: "resolve" },
                          true,
                          [note, confirm],
                        )
                      }
                    >
                      {t("Закрыть", "Resolve")}
                    </Button>
                  )}
                </div>
              ) : null,
          },
        ]}
      />
      <section className="panel">
        <h3>{t("Защищённые соединения", "Protected connections")}</h3>
        {overview.data?.nodes.length === 0 && (
          <p>{t("Узлы ещё не подключены", "No enrolled nodes")}</p>
        )}
        {overview.data?.infrastructure_reasons.map((r) => (
          <p key={`${r.code}-${r.node_id}`} className="muted">
            {r.code} · {r.node_id} · {r.source} · {date(r.observed_at)}
          </p>
        ))}
        <div className="protection-connections">
          {overview.data?.nodes.map((n) => (
            <div key={n.id}>
              <strong>{n.hostname}</strong> · {n.status}
              <small className="block">
                {n.transport_security} · {date(n.transport_checked_at)} ·
                heartbeat {date(n.last_heartbeat)}
              </small>
            </div>
          ))}
        </div>
        <p className="muted">
          {t(
            "TLS подтверждается на слушателе или доверенном прокси. Если сервер не может подтвердить транспорт, показывается неизвестное состояние. Online определяется свежим heartbeat.",
            "TLS is reported at the listener or trusted proxy. An unverifiable transport remains unknown. Online status depends on fresh heartbeats.",
          )}
        </p>
      </section>
      {isAdmin && overview.data && (
        <section className="panel">
          <h3>{t("Политика выполнения", "Execution policy")}</h3>
          <p>
            {t(
              "Steam/CS2 требуют ручного подтверждения входа на конкретном узле. Подтверждённые ограничения, ручная пауза и конфликты сессий блокируют запуск. Изолированный тестовый движок работает независимо.",
              "Steam/CS2 require a node-bound manual sign-in attestation. Confirmed restrictions, manual holds and conflicting sessions block execution. The isolated test engine remains independent.",
            )}
          </p>
          <Button
            variant="secondary"
            onClick={() =>
              setForm({
                title: t("Политика проверок", "Verification policy"),
                description: t(
                  "Изменение немедленно останавливает операции, не соответствующие новой политике.",
                  "Changes immediately stop operations that violate the new policy.",
                ),
                fields: [
                  {
                    name: "require_fresh_checks",
                    label: t(
                      "Требовать свежую успешную проверку Steam API",
                      "Require a fresh successful Steam API check",
                    ),
                    type: "checkbox",
                    value: overview.data!.policy.require_fresh_checks,
                  },
                  {
                    name: "check_interval_seconds",
                    label: t(
                      "Интервал проверок, сек",
                      "Check interval, seconds",
                    ),
                    type: "number",
                    min: 300,
                    value: overview.data!.policy.check_interval_seconds,
                  },
                  {
                    name: "max_check_age_seconds",
                    label: t(
                      "Максимальный возраст сведений, сек",
                      "Maximum evidence age, seconds",
                    ),
                    type: "number",
                    min: 300,
                    value: overview.data!.policy.max_check_age_seconds,
                  },
                ],
                submit: (v) =>
                  api("/protection/policy", {
                    method: "PATCH",
                    body: JSON.stringify(v),
                  }),
              })
            }
          >
            {t("Изменить политику", "Edit policy")}
          </Button>
        </section>
      )}
      <ActionDialog
        spec={form}
        onClose={() => setForm(null)}
        onSuccess={() => {
          void client.invalidateQueries({
            predicate: (q) => q.queryKey[0] !== "me",
          });
          setNotice(
            t(
              "Операция выполнена. Проверки Steam выполняются в фоновой очереди; результат появится после ответа API.",
              "Operation accepted. Steam checks run in the background; evidence updates after the API response.",
            ),
          );
        }}
      />
    </div>
  );
}
