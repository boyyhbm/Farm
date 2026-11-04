"use client";
import { HealthBadge } from "@/components/health-badge";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, Download, ScanLine } from "lucide-react";
import { api, download } from "@/services/api";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/data-table";
import { ActionDialog, type FormSpec } from "@/components/action-dialog";
import { date } from "@/lib/utils";
import type { Page, Row, Field } from "@/types";

type Observation = {
  code: string;
  status: string;
  source: string;
  checked_at: string | null;
  explanation: string;
};
type Health = {
  status: string;
  source: string;
  checked_at: string;
  explanation: string;
  observations: Observation[];
};
type Integrity = {
  id: string;
  status: string;
  checked_at: string;
  source: string;
  report: {
    baseline_trust: string;
    files_checked: number;
    game_files_status: string;
    findings: {
      code: string;
      status: string;
      component: string;
      explanation: string;
    }[];
  };
};
type SafeNode = Row & {
  hostname: string;
  agent_version: string;
  last_heartbeat: string | null;
  metrics: Record<string, unknown>;
  safety: { state: string; reasons: string[]; evaluated_at?: string };
  integrity: Integrity | null;
};
type Engine = {
  version: string;
  service: {
    status: string;
    source: string;
    checked_at: string | null;
    error: string | null;
  };
  nodes_requiring_review: number;
  queue: Record<string, number>;
  policy: Record<string, string | number | boolean>;
  manager_integrity: Integrity | null;
};
const policyLabels: Record<string, [string, string]> = {
  cpu_temperature_limit: [
    "Критическая температура CPU, °C",
    "Critical CPU temperature, °C",
  ],
  gpu_temperature_limit: [
    "Критическая температура GPU, °C",
    "Critical GPU temperature, °C",
  ],
  ram_percent_limit: [
    "Критическое использование RAM, %",
    "Critical RAM usage, %",
  ],
  min_ram_available_gb: [
    "Минимум свободной RAM, ГБ",
    "Minimum available RAM, GiB",
  ],
  min_disk_free_gb: ["Минимум места на диске, ГБ", "Minimum disk space, GiB"],
  cpu_percent_limit: ["Критическая загрузка CPU, %", "Critical CPU usage, %"],
  sustained_cpu_seconds: [
    "Продолжительность высокой нагрузки, сек",
    "High-load duration, seconds",
  ],
  require_integrity_for_games: [
    "Требовать свежую проверку компонентов перед Steam/CS2",
    "Require fresh component checks before Steam/CS2",
  ],
  max_integrity_age_seconds: [
    "Максимальный возраст проверки, сек",
    "Maximum integrity age, seconds",
  ],
  minimum_agent_version: [
    "Минимальная версия агента (пустое поле отключает ограничение)",
    "Minimum agent version (empty disables the limit)",
  ],
};
export default function SafetyEngine({
  ru,
  role,
}: {
  ru: boolean;
  role: string;
}) {
  const t = (r: string, e: string) => (ru ? r : e);
  const isAdmin = role === "admin",
    canOperate = isAdmin || role === "operator";
  const client = useQueryClient();
  const [form, setForm] = useState<FormSpec | null>(null),
    [notice, setNotice] = useState("");
  const [q, setQ] = useState(""),
    [healthStatus, setHealthStatus] = useState(""),
    [healthPage, setHealthPage] = useState(1);
  const [nodeQ, setNodeQ] = useState(""),
    [nodeState, setNodeState] = useState(""),
    [nodePage, setNodePage] = useState(1);
  const [decisionQ, setDecisionQ] = useState(""),
    [decisionPage, setDecisionPage] = useState(1),
    [filterNode, setFilterNode] = useState("");
  const [integrityPage, setIntegrityPage] = useState(1),
    [integrityStatus, setIntegrityStatus] = useState("");
  const [selected, setSelected] = useState<Health | null>(null);
  const status = useQuery({
    queryKey: ["safety-status"],
    queryFn: () => api<Engine>("/protection/safety/status"),
    refetchInterval: 10000,
  });
  const health = useQuery({
    queryKey: ["account-health", q, healthStatus, healthPage],
    queryFn: () =>
      api<Page & { health_counts: Record<string, number> }>(
        `/protection/health/accounts?q=${encodeURIComponent(q)}&page=${healthPage}&page_size=25${healthStatus ? "&health_status=" + healthStatus : ""}`,
      ),
    refetchInterval: 15000,
  });
  const nodes = useQuery({
    queryKey: ["node-safety", nodeQ, nodeState, nodePage],
    queryFn: () =>
      api<Page>(
        `/protection/nodes/safety?q=${encodeURIComponent(nodeQ)}&page=${nodePage}&page_size=25${nodeState ? "&state=" + nodeState : ""}`,
      ),
    refetchInterval: 10000,
  });
  const decisions = useQuery({
    queryKey: ["safety-decisions", decisionPage, decisionQ, filterNode],
    queryFn: () =>
      api<Page>(
        `/protection/decisions?page=${decisionPage}&page_size=25&q=${encodeURIComponent(decisionQ)}${filterNode ? "&node_id=" + encodeURIComponent(filterNode) : ""}`,
      ),
    refetchInterval: 10000,
  });
  const integrity = useQuery({
    queryKey: ["integrity-checks", integrityPage, integrityStatus, filterNode],
    queryFn: () =>
      api<Page>(
        `/protection/integrity?page=${integrityPage}&page_size=10${integrityStatus ? "&result=" + integrityStatus : ""}${filterNode ? "&node_id=" + encodeURIComponent(filterNode) : ""}`,
      ),
    refetchInterval: 15000,
  });
  const services = useQuery({
    queryKey: ["safety-system-services"],
    queryFn: () => api<Row>("/system/status"),
    refetchInterval: 15000,
  });
  const note: Field = {
    name: "note",
    type: "textarea",
    required: true,
    label: t("Основание проверки, без секретов", "Review reason, no secrets"),
  };
  const confirmed: Field = {
    name: "confirmed",
    type: "checkbox",
    required: true,
    label: t(
      "Я проверил условия и подтверждаю действие",
      "I have reviewed the conditions and confirm",
    ),
  };
  const action = (
    title: string,
    description: string,
    path: string,
    destructive = false,
  ) =>
    setForm({
      title,
      description,
      fields: [note, confirmed],
      destructive,
      submit: (v) => api(path, { method: "POST", body: JSON.stringify(v) }),
    });
  const exportReport = async (kind: string, format: string) => {
    try {
      await download(
        `/protection/reports/export?kind=${kind}&format=${format}&q=${encodeURIComponent(kind === "health" ? q : kind === "nodes" ? nodeQ : decisionQ)}${filterNode ? "&node_id=" + encodeURIComponent(filterNode) : ""}`,
        `farm-protection-${kind}.${format}`,
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    }
  };
  const exports = (kind: string) => (
    <div className="row-actions">
      <Button
        size="sm"
        variant="ghost"
        onClick={() => void exportReport(kind, "csv")}
      >
        <Download size={13} />
        CSV
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => void exportReport(kind, "json")}
      >
        JSON
      </Button>
    </div>
  );
  return (
    <section className="safety-engine" aria-label="Farm Protection Engine 2.0">
      <div className="protection-actions">
        <h2>
          <ShieldCheck size={21} /> Farm Protection Engine 2.0
        </h2>
        <HealthBadge value={status.data?.service.status} />
      </div>
      {status.error && (
        <p role="alert" className="error-banner">
          {status.error.message}
        </p>
      )}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      <div className="protection-grid">
        <article className="panel">
          <small>{t("Safety Controller", "Safety Controller")}</small>
          <strong className="block">
            {status.data?.service.status || "UNKNOWN"}
          </strong>
          <small>
            {date(status.data?.service.checked_at)} ·{" "}
            {status.data?.service.source}
          </small>
        </article>
        <article className="panel">
          <small>{t("Узлы на проверке", "Nodes requiring review")}</small>
          <h2>{status.data?.nodes_requiring_review ?? "—"}</h2>
        </article>
        <article className="panel">
          <small>
            {t("Очередь / активные / неизвестные", "Queued / active / unknown")}
          </small>
          <h2>
            {status.data
              ? `${status.data.queue.QUEUED || 0} / ${(status.data.queue.RUNNING || 0) + (status.data.queue.STARTING || 0)} / ${status.data.queue.UNKNOWN || 0}`
              : "—"}
          </h2>
        </article>
      </div>
      <p className="muted">
        {t(
          "Опасная операция не возобновляется автоматически. Остальные узлы продолжают разрешённую работу. Отсутствующие показания температуры остаются неизвестными.",
          "Unsafe work never resumes automatically. Other nodes continue permitted work. Missing temperature readings remain unknown.",
        )}
      </p>
      <section className="panel">
        <h3>{t("Службы главного компьютера", "Main computer services")}</h3>
        {isAdmin && (
          <Button
            size="sm"
            variant="secondary"
            onClick={() =>
              action(
                t("Проверка компонентов Manager", "Check Manager components"),
                t(
                  "Сверить поставляемые компоненты с эталоном. Очередь после исправления автоматически не возобновляется.",
                  "Compare packaged components with their baseline. The queue never resumes automatically after repair.",
                ),
                "/protection/safety/check-manager",
              )
            }
          >
            {t("Проверить Manager", "Inspect Manager")}
          </Button>
        )}
        {services.error ? (
          <p role="alert">{services.error.message}</p>
        ) : (
          <div className="protection-connections">
            {["backend", "database", "queue", "worker"].map((key) => (
              <div key={key}>
                <strong>{key}</strong> ·{" "}
                {String(
                  (services.data?.[key] as Row | undefined)?.status ||
                    "UNKNOWN",
                )}
              </div>
            ))}
          </div>
        )}
        <p>
          {t("Целостность Manager", "Manager integrity")}:{" "}
          <HealthBadge value={status.data?.manager_integrity?.status} />
          {" · "}
          {date(status.data?.manager_integrity?.checked_at)} ·{" "}
          {status.data?.manager_integrity?.report.baseline_trust ||
            "not_available"}
        </p>
        {status.data?.manager_integrity?.report.findings.map((f, i) => (
          <small className="block" key={i}>
            {f.code}: {f.explanation}
          </small>
        ))}
      </section>
      <div className="protection-actions">
        <h3>Account Health</h3>
        <label>
          {t("Состояние", "State")}{" "}
          <select
            value={healthStatus}
            onChange={(e) => {
              setHealthStatus(e.target.value);
              setHealthPage(1);
            }}
          >
            <option value="">{t("Все", "All")}</option>
            {["Healthy", "Warning", "Restricted", "Banned", "Unknown"].map(
              (s) => (
                <option key={s}>{s}</option>
              ),
            )}
          </select>
        </label>
        {exports("health")}
      </div>
      <p className="muted">
        {health.data
          ? Object.entries(health.data.health_counts)
              .map(([key, value]) => `${key}: ${value}`)
              .join(" · ")
          : t("Проверка наблюдений…", "Checking observations…")}
      </p>
      <DataTable
        data={health.data}
        loading={health.isPending}
        error={health.error}
        page={healthPage}
        onPage={setHealthPage}
        search={q}
        onSearch={(v) => {
          setQ(v);
          setHealthPage(1);
        }}
        columns={[
          { key: "steam_id", label: "SteamID64" },
          {
            key: "health",
            label: t("Состояние", "Health"),
            render: (r) => <HealthBadge value={(r.health as Health).status} />,
          },
          {
            key: "checked_at",
            label: t("Сведения Steam", "Steam evidence"),
            render: (r) => (
              <span>
                {String(r.source || "—")}
                <small className="block">{date(r.checked_at)}</small>
              </span>
            ),
          },
          {
            key: "inventory",
            label: t("Источник оценки", "Assessment source"),
            render: (r) => (
              <span>
                {(r.health as Health).source}
                <small className="block">
                  {date((r.health as Health).checked_at)}
                </small>
              </span>
            ),
          },
          {
            key: "actions",
            label: "",
            render: (r) => (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSelected(r.health as Health)}
              >
                {t("Объяснение", "Explain")}
              </Button>
            ),
          },
        ]}
      />
      {selected && (
        <section className="panel">
          <div className="protection-actions">
            <h3>{t("Обоснование состояния", "Health evidence")}</h3>
            <Button variant="ghost" onClick={() => setSelected(null)}>
              {t("Закрыть", "Close")}
            </Button>
          </div>
          <p>{selected.explanation}</p>
          {selected.observations.map((o, i) => (
            <div className="safety-observation" key={i}>
              <HealthBadge value={o.status} />
              <strong>{o.code}</strong>
              <small>
                {o.source} · {date(o.checked_at)}
              </small>
              <p>{o.explanation}</p>
            </div>
          ))}
        </section>
      )}
      <div className="protection-actions">
        <h3>{t("Безопасность физических узлов", "Distributed node safety")}</h3>
        <select
          aria-label={t("Состояние узла", "Node safety state")}
          value={nodeState}
          onChange={(e) => {
            setNodeState(e.target.value);
            setNodePage(1);
          }}
        >
          <option value="">{t("Все состояния", "All states")}</option>
          {["UNKNOWN", "READY", "WARNING", "REVIEW_REQUIRED", "RELEASING"].map(
            (s) => (
              <option key={s}>{s}</option>
            ),
          )}
        </select>
        {exports("nodes")}
      </div>
      <DataTable
        data={nodes.data}
        loading={nodes.isPending}
        error={nodes.error}
        page={nodePage}
        onPage={setNodePage}
        search={nodeQ}
        onSearch={(v) => {
          setNodeQ(v);
          setNodePage(1);
        }}
        columns={[
          {
            key: "hostname",
            label: "Node",
            render: (r) => (
              <span>
                {String(r.hostname)}
                <small className="block">
                  {r.id} · {String(r.agent_version)}
                </small>
              </span>
            ),
          },
          {
            key: "safety",
            label: t("Режим безопасности", "Safety state"),
            render: (r) => (
              <span>
                <HealthBadge value={(r as SafeNode).safety.state} />
                <small className="block">
                  {(r as SafeNode).safety.reasons.join(", ") || "—"}
                </small>
              </span>
            ),
          },
          {
            key: "metrics",
            label: "CPU / GPU °C · RAM",
            render: (r) => {
              const m = (r as SafeNode).metrics;
              return (
                <span>
                  {String(m.cpu_temperature ?? "Unknown")} /{" "}
                  {String(m.gpu_temperature ?? "Unknown")} °C ·{" "}
                  {String(m.ram_percent ?? "Unknown")}%
                  <small className="block">
                    {String(m.cpu_temperature_source || "unavailable")} /{" "}
                    {String(m.gpu_temperature_source || "unavailable")} ·{" "}
                    {date(r.last_heartbeat)}
                  </small>
                </span>
              );
            },
          },
          {
            key: "integrity",
            label: t("Целостность", "Integrity"),
            render: (r) => (
              <span>
                <HealthBadge value={(r as SafeNode).integrity?.status} />
                <small className="block">
                  {date((r as SafeNode).integrity?.checked_at)}
                  {r.last_error ? (
                    <small className="block" role="alert">
                      {String(r.last_error)}
                    </small>
                  ) : null}
                </small>
              </span>
            ),
          },
          {
            key: "actions",
            label: "",
            render: (r) => (
              <div className="row-actions">
                {isAdmin && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      action(
                        t(
                          "Приостановить узел для проверки",
                          "Hold node for review",
                        ),
                        t(
                          "Остановить управляемые задания только этого узла. Локальная блокировка сохранится после перезапуска. Остальные узлы продолжат работу.",
                          "Stop only this node's managed jobs. The local hold persists across restart; other nodes continue.",
                        ),
                        `/protection/nodes/${r.id}/hold`,
                        true,
                      )
                    }
                  >
                    {t("На проверку", "Hold for review")}
                  </Button>
                )}
                {canOperate && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      action(
                        t(
                          "Проверка целостности узла",
                          "Inspect node integrity",
                        ),
                        t(
                          "Проверить поставляемые компоненты, подписи и доступный эталон игровых файлов. Результат появится после подтверждения агентом.",
                          "Inspect packaged components, signatures and the available game baseline. Results require an actual agent response.",
                        ),
                        `/protection/nodes/${r.id}/integrity-check`,
                      )
                    }
                  >
                    <ScanLine size={13} />
                    {t("Проверить", "Inspect")}
                  </Button>
                )}
                {isAdmin && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      action(
                        t(
                          "Эталон после проверки Steam",
                          "Baseline after Steam verification",
                        ),
                        t(
                          "Сначала завершите официальную проверку файлов через Steam. Подтвердите, что Steam и CS2 установлены корректно, а управляемые процессы остановлены. Эталон охватывает выбранные EXE/DLL, а не каждый игровой ресурс.",
                          "First complete official Steam file verification. Confirm the installation and stop managed processes. The baseline covers selected EXE/DLL files, not every game resource.",
                        ),
                        `/protection/nodes/${r.id}/game-baseline`,
                        true,
                      )
                    }
                  >
                    {t("Эталон", "Baseline")}
                  </Button>
                )}
                {isAdmin && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      action(
                        t(
                          "Подтвердить исправление обновления",
                          "Confirm update repair",
                        ),
                        t(
                          "После ремонта или отката установщика подтвердите проверку. Это снимет отметку сбоя обновления; остальные неисправности и блокировка узла сохраняются.",
                          "After repair or rollback, confirm the review. This clears the update-failure marker; other faults and the node hold remain.",
                        ),
                        `/protection/nodes/${r.id}/repair-confirmation`,
                        true,
                      )
                    }
                  >
                    {t("Ремонт проверен", "Repair reviewed")}
                  </Button>
                )}
                {isAdmin &&
                  ["REVIEW_REQUIRED", "RELEASING"].includes(
                    (r as SafeNode).safety.state,
                  ) && (
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={(r as SafeNode).safety.state === "RELEASING"}
                      onClick={() =>
                        action(
                          t(
                            "Возврат узла после проверки",
                            "Release reviewed node",
                          ),
                          t(
                            "Нужны свежая проверка целостности и безопасные показания. Агент повторит локальные проверки. Отменённые задания и авторизация Steam автоматически не восстанавливаются.",
                            "Fresh integrity evidence and safe readings are required. The agent repeats local checks. Cancelled jobs and Steam sign-in attestations are not restored automatically.",
                          ),
                          `/protection/nodes/${r.id}/release`,
                          true,
                        )
                      }
                    >
                      {(r as SafeNode).safety.state === "RELEASING"
                        ? t("Ожидаю агента", "Awaiting agent")
                        : t("Разрешить работу", "Release")}
                    </Button>
                  )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setFilterNode(r.id);
                    setDecisionPage(1);
                    setIntegrityPage(1);
                  }}
                >
                  {t("История узла", "Node history")}
                </Button>
              </div>
            ),
          },
        ]}
      />
      <div className="protection-actions">
        <h3>{t("Решения Safety Controller", "Safety Controller decisions")}</h3>
        <label>
          Node ID{" "}
          <input
            value={filterNode}
            onChange={(e) => {
              setFilterNode(e.target.value);
              setDecisionPage(1);
              setIntegrityPage(1);
            }}
            placeholder={t("Все узлы", "All nodes")}
          />
        </label>
        {exports("decisions")}
      </div>
      <DataTable
        data={decisions.data}
        loading={decisions.isPending}
        error={decisions.error}
        page={decisionPage}
        onPage={setDecisionPage}
        search={decisionQ}
        onSearch={(v) => {
          setDecisionQ(v);
          setDecisionPage(1);
        }}
        columns={[
          {
            key: "created_at",
            label: t("Время", "Time"),
            render: (r) => date(r.created_at),
          },
          { key: "action", label: t("Решение", "Decision") },
          { key: "reason", label: t("Причина", "Reason") },
          { key: "node_id", label: "Node ID" },
          { key: "account_id", label: "Account ID" },
          { key: "job_id", label: "Job ID" },
          { key: "source", label: t("Источник", "Source") },
          { key: "actor", label: t("Инициатор", "Actor") },
        ]}
      />
      <div className="protection-actions">
        <h3>{t("История проверок целостности", "Integrity check history")}</h3>
        <select
          aria-label={t("Результат целостности", "Integrity result")}
          value={integrityStatus}
          onChange={(e) => {
            setIntegrityStatus(e.target.value);
            setIntegrityPage(1);
          }}
        >
          <option value="">{t("Все результаты", "All results")}</option>
          {["PASS", "WARNING", "FAILED", "UNKNOWN"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        {exports("integrity")}
      </div>
      <DataTable
        data={integrity.data}
        loading={integrity.isPending}
        error={integrity.error}
        page={integrityPage}
        onPage={setIntegrityPage}
        columns={[
          {
            key: "checked_at",
            label: t("Проверено", "Observed"),
            render: (r) => date(r.checked_at),
          },
          { key: "node_id", label: "Node ID" },
          {
            key: "status",
            label: t("Результат", "Result"),
            render: (r) => <HealthBadge value={r.status} />,
          },
          { key: "source", label: t("Источник", "Source") },
          {
            key: "report",
            label: t("Проверки и ограничения", "Findings and limits"),
            render: (r) => {
              const report = r.report as Integrity["report"];
              return (
                <div>
                  <small>
                    {report.baseline_trust} · {report.files_checked}{" "}
                    {t("файлов", "files")} · {report.game_files_status}
                  </small>
                  {report.findings.map((f, i) => (
                    <small className="block" key={i}>
                      {f.status} · {f.component} · {f.code}: {f.explanation}
                    </small>
                  ))}
                </div>
              );
            },
          },
        ]}
      />
      {isAdmin && status.data && (
        <section className="panel">
          <h3>{t("Политика автономной защиты", "Autonomous safety policy")}</h3>
          <p>
            {t(
              "Пороги применяются только к наблюдаемым значениям. Критическая блокировка сохраняется до проверки администратора и агента.",
              "Thresholds apply only to observed values. A critical hold persists until administrator and agent review.",
            )}
          </p>
          <Button
            variant="secondary"
            onClick={() =>
              setForm({
                title: t("Изменить Safety Policy", "Edit Safety Policy"),
                description: t(
                  "Более строгие пороги могут немедленно остановить затронутые операции.",
                  "Stricter thresholds may immediately stop affected work.",
                ),
                fields: [
                  ...Object.entries(status.data!.policy)
                    .filter(([key]) => key !== "id")
                    .map(
                      ([key, value]) =>
                        ({
                          name: key,
                          step: typeof value === "number" ? "any" : undefined,
                          label: policyLabels[key]?.[ru ? 0 : 1] || key,
                          type:
                            typeof value === "boolean"
                              ? "checkbox"
                              : typeof value === "number"
                                ? "number"
                                : "text",
                          value,
                        }) as Field,
                    ),
                  note,
                  confirmed,
                ],
                submit: (v) =>
                  api("/protection/safety/policy", {
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
              "Запрос сохранён. Следите за фактическим подтверждением агента и состоянием узла.",
              "Request recorded. Follow the actual agent acknowledgement and node state.",
            ),
          );
        }}
      />
    </section>
  );
}
