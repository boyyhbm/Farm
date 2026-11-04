"use client";
import { useEffect, useState, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  LayoutDashboard,
  Users,
  Monitor,
  Play,
  CalendarClock,
  Gift,
  Package,
  ChartNoAxesCombined,
  ScrollText,
  Settings,
  ShoppingCart,
  Plus,
  LogOut,
  ChevronsUpDown,
  ArrowUpRight,
  Square,
  OctagonX,
  Pause,
  RefreshCw,
  Download,
  Upload,
  Menu,
  X,
  ShieldCheck,
  Activity,
  ChevronRight,
  CheckCircle2,
  AlertTriangle,
  Copy,
  ExternalLink,
} from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  BarChart,
  Bar,
} from "recharts";
import { Button } from "@/components/ui/button";
import {
  AgentInstaller,
  SystemManagement,
} from "@/components/system-management";
import { DataTable, Status, display } from "@/components/data-table";
import { ActionDialog, type FormSpec } from "@/components/action-dialog";
import { api, ApiError, setCsrf, download, allPages } from "@/services/api";
import { useUI } from "@/lib/store";
import { money, date } from "@/lib/utils";
import type { User, Page, Row, Field, Section } from "@/types";
import Protection, { ProtectionSummary } from "@/components/protection";
const menu = [
  { id: "dashboard", icon: LayoutDashboard, en: "Overview", ru: "Обзор" },
  { id: "accounts", icon: Users, en: "Accounts", ru: "Аккаунты" },
  { id: "nodes", icon: Monitor, en: "Nodes", ru: "Компьютеры" },
  { id: "sessions", icon: Play, en: "Sessions", ru: "Сессии" },
  { id: "scheduler", icon: CalendarClock, en: "Scheduler", ru: "Планировщик" },
  { id: "drops", icon: Gift, en: "Weekly drops", ru: "Недельные награды" },
  { id: "inventory", icon: Package, en: "Inventory", ru: "Инвентарь" },
  { id: "market", icon: ShoppingCart, en: "Market", ru: "Рынок" },
  {
    id: "analytics",
    icon: ChartNoAxesCombined,
    en: "Analytics",
    ru: "Аналитика",
  },
  { id: "protection", icon: ShieldCheck, en: "Protection", ru: "Защита" },
  { id: "logs", icon: ScrollText, en: "Audit logs", ru: "Журнал аудита" },
  { id: "settings", icon: Settings, en: "Settings", ru: "Настройки" },
] as const;
const ACTIVE = ["QUEUED", "STARTING", "RUNNING", "STOPPING", "UNKNOWN"];
const text = (v: unknown) => String(v ?? "");
const items = (data: Page | undefined) => data?.items || [];
const jobFields = (accounts: Row[], nodes: Row[], ru: boolean): Field[] => [
  {
    name: "kind",
    label: ru ? "Тип задания" : "Job type",
    type: "select",
    required: true,
    value: "test",
    options: [
      { value: "test", label: "Isolated test engine" },
      { value: "steam", label: "Steam · manual sign-in" },
      { value: "cs2", label: "CS2 · permitted session" },
    ],
  },
  {
    name: "account_id",
    label: ru ? "Аккаунт (для Steam/CS2)" : "Account (Steam/CS2)",
    type: "select",
    options: accounts.map((r) => ({
      value: r.id,
      label: text(r.nickname || r.steam_id),
    })),
  },
  {
    name: "node_id",
    label: ru
      ? "Компьютер (пусто = автоматически)"
      : "Node (empty = automatic)",
    type: "select",
    options: nodes
      .filter((n) => !n.revoked)
      .map((n) => ({ value: n.id, label: text(n.hostname) })),
  },
  {
    name: "duration_seconds",
    label: ru ? "Длительность теста, с" : "Test duration, seconds",
    type: "number",
    value: 60,
    min: 1,
  },
  { name: "seed", label: "Test seed", type: "number", value: 1, min: 0 },
  {
    name: "priority",
    label: ru ? "Приоритет" : "Priority",
    type: "number",
    value: 0,
  },
  {
    name: "timeout_seconds",
    label: ru ? "Тайм-аут, с" : "Timeout, seconds",
    type: "number",
    value: 300,
    min: 10,
  },
  {
    name: "max_attempts",
    label: ru ? "Максимум попыток" : "Maximum attempts",
    type: "number",
    value: 3,
    min: 1,
  },
  {
    name: "min_ram_gb",
    label: ru ? "Минимум свободной RAM, GB" : "Minimum available RAM, GB",
    type: "number",
    value: 0,
    min: 0,
    step: "0.1",
  },
  {
    name: "max_cpu_percent",
    label: ru ? "Максимальная загрузка CPU, %" : "Maximum CPU load, %",
    type: "number",
    value: 95,
    min: 1,
  },
  {
    name: "scheduled_at",
    label: ru
      ? "Запуск по времени (необязательно, местное время)"
      : "Scheduled start (optional, local time)",
    type: "datetime-local",
  },
];
function jobBody(v: Record<string, string | number | boolean>) {
  return {
    kind: v.kind,
    account_id: v.account_id || null,
    node_id: v.node_id || null,
    priority: v.priority,
    timeout_seconds: v.timeout_seconds,
    max_attempts: v.max_attempts,
    scheduled_at: v.scheduled_at
      ? new Date(text(v.scheduled_at)).toISOString()
      : null,
    payload: {
      duration_seconds: v.duration_seconds,
      seed: v.seed,
      min_ram_gb: v.min_ram_gb,
      max_cpu_percent: v.max_cpu_percent,
    },
  };
}
function Chart({
  data,
  dataKey = "value",
  color = "#51c598",
}: {
  data: Record<string, unknown>[];
  dataKey?: string;
  color?: string;
}) {
  return data.length ? (
    <div className="chart">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={data}
          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
        >
          <defs>
            <linearGradient id={`g-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.23} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid
            stroke="#26302f"
            strokeDasharray="3 4"
            vertical={false}
          />
          <XAxis
            dataKey="date"
            tick={{ fill: "#7f8a89", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            tick={{ fill: "#7f8a89", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={45}
          />
          <Tooltip
            contentStyle={{
              background: "#19201f",
              border: "1px solid #33403d",
              borderRadius: 8,
            }}
          />
          <Area
            type="monotone"
            dataKey={dataKey}
            stroke={color}
            strokeWidth={2}
            fill={`url(#g-${dataKey})`}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  ) : (
    <div className="chart empty-chart">
      <ChartNoAxesCombined size={28} />
      <p>No history yet / История пока отсутствует</p>
    </div>
  );
}
export default function FarmDashboard({ initialSection }: { initialSection?: Section } = {}) {
  const client = useQueryClient();
  const { locale, setLocale, section, setSection } = useUI();
  const ru = locale === "ru";
  useEffect(() => { if (initialSection) setSection(initialSection); }, [initialSection, setSection]);
  const t = (a: string, b: string) => (ru ? a : b);
  const [authenticated, setAuthenticated] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [form, setForm] = useState<FormSpec | null>(null);
  const [notice, setNotice] = useState<{
    message: string;
    error?: boolean;
  } | null>(null);
  const [secret, setSecret] = useState("");
  const [selection, setSelection] = useState<string[]>([]);
  const [sessionSelection, setSessionSelection] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [accountFilters, setAccountFilters] = useState({
    archived: "false",
    prime_status: "",
    node_id: "",
    tag: "",
  });
  const [inventoryQ, setInventoryQ] = useState("");
  const [accountId, setAccountId] = useState("");
  const [live, setLive] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api<User>("/auth/me"),
    retry: false,
    refetchInterval: false,
  });
  useEffect(() => {
    if (me.data) {
      setCsrf(me.data.csrf_token);
      setAuthenticated(true);
    } else if (me.error instanceof ApiError && me.error.status === 401) {
      setAuthenticated(false);
    }
  }, [me.data, me.error]);
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);
  useEffect(() => {
    setPage(1);
    setQ("");
  }, [section]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 10000);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (!authenticated) return;
    let ws: WebSocket;
    let timer: ReturnType<typeof setTimeout>;
    let disposed = false;
    function connect() {
      const base =
        process.env.NEXT_PUBLIC_WS_URL ||
        `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;
      ws = new WebSocket(`${base}/ws/live`);
      ws.onopen = () => setLive(true);
      ws.onmessage = () => {
        void client.invalidateQueries({
          predicate: (x) =>
            !["me", "accounts-options", "groups"].includes(
              String(x.queryKey[0]),
            ),
        });
      };
      ws.onclose = () => {
        setLive(false);
        if (!disposed) timer = setTimeout(connect, 5000);
      };
      ws.onerror = () => ws.close();
    }
    connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      ws?.close();
      setLive(false);
    };
  }, [authenticated, client]);
  const accounts = useQuery({
    queryKey: ["accounts-options"],
    queryFn: () => allPages("/accounts"),
    refetchInterval: false,
    staleTime: 300000,
    enabled: authenticated,
  });
  const nodes = useQuery({
    queryKey: ["nodes-options"],
    queryFn: () => allPages("/nodes"),
    enabled: authenticated,
  });
  const periods = useQuery({
    queryKey: ["periods"],
    queryFn: () => allPages("/drops/periods"),
    enabled: authenticated && section === "drops",
  });
  const groups = useQuery({
    queryKey: ["groups"],
    queryFn: () => api<{ items: Row[] }>("/groups"),
    enabled: authenticated,
  });
  const control = useQuery({
    queryKey: ["control"],
    queryFn: () => api<{ paused: boolean; policy: string }>("/control"),
    enabled: authenticated,
  });
  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: () => api("/dashboard/overview"),
    enabled: authenticated && section === "dashboard",
  });
  const analytics = useQuery({
    queryKey: ["analytics"],
    queryFn: () => api("/analytics/overview"),
    enabled:
      authenticated && (section === "analytics" || section === "dashboard"),
  });
  const notifications = useQuery({
    queryKey: ["notifications", page],
    queryFn: () => api<Page>(`/notifications?page=${page}&page_size=25`),
    enabled: authenticated && section === "settings",
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api("/settings"),
    enabled: authenticated && section === "settings",
  });
  const inventory = useQuery({
    queryKey: ["inventory", accountId],
    queryFn: () =>
      api<{ items: Row[]; snapshots: Row[]; changes: Row[] }>(
        `/inventory/${accountId}`,
      ),
    enabled: authenticated && section === "inventory" && !!accountId,
  });
  const endpoint = (
    {
      accounts: "/accounts",
      nodes: "/nodes",
      sessions: "/sessions",
      scheduler: "/jobs",
      drops: "/drops",
      market: "/market",
      logs: "/logs",
      settings: "/users",
    } as Partial<Record<Section, string>>
  )[section];
  const list = useQuery({
    queryKey: ["list", section, page, q, accountFilters],
    queryFn: () =>
      api<Page>(
        `${endpoint}?page=${page}&page_size=25&q=${encodeURIComponent(q)}${section === "accounts" ? "&" + new URLSearchParams(Object.fromEntries(Object.entries(accountFilters).filter(([, v]) => !!v))).toString() : ""}`,
      ),
    enabled:
      authenticated &&
      !!endpoint &&
      (section !== "settings" || me.data?.role === "admin"),
  });
  const scheduleList = useQuery({
    queryKey: ["schedules"],
    queryFn: () => allPages("/schedules"),
    enabled: authenticated && section === "scheduler",
  });
  const expenses = useQuery({
    queryKey: ["expenses", page],
    queryFn: () => api<Page>(`/expenses?page=${page}&page_size=25`),
    enabled: authenticated && section === "analytics",
  });
  const revenues = useQuery({
    queryKey: ["revenues", page],
    queryFn: () => api<Page>(`/revenues?page=${page}&page_size=25`),
    enabled: authenticated && section === "analytics",
  });
  const notify = (message: string, error = false) =>
    setNotice({ message, error });
  const refresh = () =>
    client.invalidateQueries({ predicate: (x) => x.queryKey[0] !== "me" });
  const confirm = (
    title: string,
    description: string,
    action: () => Promise<unknown>,
    destructive = false,
  ) => setForm({ title, description, fields: [], destructive, submit: action });
  const accountOptions = items(accounts.data).map((a) => ({
    value: a.id,
    label: text(a.nickname || a.steam_id),
  }));
  const nodeOptions = items(nodes.data).map((n) => ({
    value: n.id,
    label: text(n.hostname),
  }));
  const createJob = () => {
    const idempotencyKey = crypto.randomUUID();
    setForm({
      title: t("Создать задание", "Create job"),
      description: t(
        "Steam требует ручного входа на выбранном ПК. Тестовый движок изолирован и не подключается к CS2.",
        "Steam requires manual sign-in on the selected PC. The test engine is isolated and never connects to CS2.",
      ),
      fields: jobFields(items(accounts.data), items(nodes.data), ru),
      submit: (v) =>
        api("/jobs", {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: JSON.stringify(jobBody(v)),
        }),
    });
  };
  const createAccount = (row?: Row) =>
    setForm({
      title: row
        ? t("Изменить аккаунт", "Edit account")
        : t("Добавить аккаунт", "Add account"),
      fields: [
        ...(!row
          ? [{ name: "steam_id", label: "SteamID64", required: true }]
          : []),
        ...(row
          ? [
              {
                name: "archived",
                label: t("Архивная запись", "Archived record"),
                type: "checkbox" as const,
                value: Boolean(row.archived),
              },
            ]
          : []),
        {
          name: "group_id",
          label: t("Группа", "Group"),
          type: "select",
          value: text(row?.group_id),
          options: (groups.data?.items || []).map((g) => ({
            value: g.id,
            label: text(g.name),
          })),
        },
        {
          name: "prime_status",
          label: "Prime",
          type: "select",
          value: text(row?.prime_status || "unknown"),
          options: ["unknown", "confirmed", "unconfirmed"].map((value) => ({
            value,
            label: value,
          })),
        },
        {
          name: "assigned_node_id",
          label: t("Назначенный компьютер", "Assigned node"),
          type: "select",
          value: text(row?.assigned_node_id),
          options: nodeOptions,
        },
        {
          name: "tags",
          label: t("Теги через запятую", "Comma-separated tags"),
          value: Array.isArray(row?.tags) ? row.tags.join(", ") : "",
        },
        {
          name: "notes",
          label: t("Комментарий", "Notes"),
          type: "textarea",
          value: text(row?.notes),
        },
      ],
      submit: (v) =>
        api(row ? `/accounts/${row.id}` : "/accounts", {
          method: row ? "PATCH" : "POST",
          body: JSON.stringify({
            ...Object.fromEntries(
              Object.entries(v).filter(([key]) => !row || key !== "steam_id"),
            ),
            group_id: v.group_id || null,
            assigned_node_id: v.assigned_node_id || null,
            tags: text(v.tags)
              .split(",")
              .map((x) => x.trim())
              .filter(Boolean),
          }),
        }),
    });
  const syncInventory = () =>
    confirm(
      t("Синхронизировать инвентарь", "Synchronize inventory"),
      t(
        "Запрос будет поставлен в отдельную очередь. Приватный инвентарь и лимиты Steam вернут диагностический статус.",
        "The request enters the inventory queue. Private inventories and Steam rate limits return a diagnostic status.",
      ),
      () => api(`/inventory/${accountId}/sync`, { method: "POST" }),
    );
  const exportFile = async (path: string, name: string) => {
    try {
      await download(path, name);
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e), true);
    }
  };
  if (me.isPending && !authenticated)
    return (
      <div className="login-screen">
        <RefreshCw className="spin" />
        <p>Connecting to control plane…</p>
      </div>
    );
  if (!authenticated)
    return (
      <div className="login-screen">
        <div className="login-card">
          <div className="brand-symbol">
            <Activity size={27} />
          </div>
          <span className="eyebrow">CS2 FARM MANAGER</span>
          <h1>{t("Центр управления", "Control center")}</h1>
          <p>
            {t(
              "Войдите для управления вашей инфраструктурой Steam.",
              "Sign in to manage your Steam infrastructure.",
            )}
          </p>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setLoginBusy(true);
              setLoginError("");
              const d = new FormData(e.currentTarget);
              try {
                const user = await api<User>("/auth/login", {
                  method: "POST",
                  body: JSON.stringify({
                    username: d.get("username"),
                    password: d.get("password"),
                  }),
                });
                setCsrf(user.csrf_token);
                client.setQueryData(["me"], user);
                setAuthenticated(true);
                await refresh();
              } catch (err) {
                setLoginError(err instanceof Error ? err.message : String(err));
              } finally {
                setLoginBusy(false);
              }
            }}
          >
            <label className="field">
              <span>{t("Имя пользователя", "Username")}</span>
              <input
                name="username"
                autoComplete="username"
                required
                autoFocus
              />
            </label>
            <label className="field">
              <span>{t("Пароль", "Password")}</span>
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
              />
            </label>
            {loginError && (
              <p className="error-banner" role="alert">
                {loginError}
              </p>
            )}
            {me.error &&
              !(me.error instanceof ApiError && me.error.status === 401) && (
                <p className="error-banner">API: {me.error.message}</p>
              )}
            <Button disabled={loginBusy} type="submit">
              {loginBusy ? t("Вход…", "Signing in…") : t("Войти", "Sign in")}
              <ArrowUpRight size={16} />
            </Button>
          </form>
          <button
            className="language-switch"
            onClick={() => setLocale(ru ? "en" : "ru")}
          >
            {ru ? "English interface" : "Русский интерфейс"}
          </button>
          <div className="login-foot">
            <ShieldCheck size={14} />
            {t(
              "Сессия защищена · секреты Steam не хранятся",
              "Secure session · Steam secrets never stored",
            )}
          </div>
        </div>
      </div>
    );
  const title = menu.find((m) => m.id === section);
  const canWrite = me.data?.role !== "viewer";
  const overviewData = overview.data || {};
  const tableProps = {
    data: list.data,
    loading: list.isPending,
    error: list.error,
    page,
    onPage: setPage,
    search: q,
    onSearch: section === "accounts" || section === "logs" ? setQ : undefined,
    empty: t(
      "Записей пока нет. Добавьте первую запись.",
      "No records yet. Add your first record.",
    ),
  };
  const actionButton = (label: string, action: () => void, danger = false) => (
    <Button
      size="sm"
      variant={danger ? "destructive" : "secondary"}
      onClick={action}
    >
      {label}
    </Button>
  );
  const metric = (
    label: string,
    value: unknown,
    icon: React.ReactNode,
    note?: string,
  ) => (
    <div className="metric-card">
      <div className="metric-head">
        <span>{label}</span>
        {icon}
      </div>
      <strong>{value == null ? "—" : String(value)}</strong>
      <small>
        {note || t("По данным системы", "From recorded system data")}
      </small>
    </div>
  );
  const resource = (label: string, value: unknown, color: string) => (
    <div className="resource">
      <div>
        <span>{label}</span>
        <strong>
          {value == null
            ? t("Нет данных", "Unavailable")
            : `${Number(value).toFixed(1)}%`}
        </strong>
      </div>
      <div className="resource-track">
        <i
          style={{
            width: `${Math.min(100, Number(value) || 0)}%`,
            background: color,
          }}
        />
      </div>
    </div>
  );
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobile ? "sidebar-open" : ""}`}>
        <div className="brand">
          <div className="brand-symbol">
            <Activity size={21} />
          </div>
          <div>
            <b>
              FARM<span>MANAGER</span>
            </b>
            <small>CS2 INFRASTRUCTURE</small>
          </div>
          <button
            className="mobile-close"
            aria-label="Close menu"
            onClick={() => setMobile(false)}
          >
            <X size={18} />
          </button>
        </div>
        <div className="workspace-pill">
          <div className="workspace-icon">FM</div>
          <div>
            <strong>{t("Моя инфраструктура", "My infrastructure")}</strong>
            <small>{t("Панель управления", "Control panel")}</small>
          </div>
          <ChevronsUpDown size={14} />
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Main navigation">
          {menu.map((m, i) => (
            <button
              className={`${section === m.id ? "active" : ""} ${i === 9 ? "nav-separated" : ""}`}
              key={m.id}
              onClick={() => {
                setSection(m.id);
                setMobile(false);
              }}
            >
              <m.icon size={17} />
              <span>{ru ? m.ru : m.en}</span>
              {m.id === "nodes" && <small>{nodes.data?.total ?? "—"}</small>}
              {section === m.id && <i />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="connection">
            <span className={`connection-dot ${live ? "online" : ""}`} />
            <span>
              {live
                ? t("Live-соединение", "Live connection")
                : t("Обновление по запросу", "Polling updates")}
            </span>
          </div>
          <div className="user">
            <div className="avatar">
              {me.data?.username.slice(0, 2).toUpperCase()}
            </div>
            <div>
              <strong>{me.data?.username}</strong>
              <small>{me.data?.role}</small>
            </div>
            <button
              aria-label="Logout"
              onClick={() =>
                confirm(
                  t("Завершить сеанс", "Sign out"),
                  t(
                    "Завершить текущую сессию панели?",
                    "End your dashboard session?",
                  ),
                  async () => {
                    await api("/auth/logout", { method: "POST" });
                    setAuthenticated(false);
                    client.clear();
                  },
                )
              }
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>
      {mobile && (
        <div className="mobile-backdrop" onClick={() => setMobile(false)} />
      )}
      <div className="main-shell">
        <header className="topbar">
          <button
            className="mobile-toggle"
            aria-label="Open menu"
            onClick={() => setMobile(true)}
          >
            <Menu size={20} />
          </button>
          <div className="breadcrumb">
            <span>Workspace</span>
            <ChevronRight size={13} />
            <strong>{ru ? title?.ru : title?.en}</strong>
          </div>
          <div className="topbar-right">
            <span className="control-state">
              <i className={control.data?.paused ? "paused" : ""} />
              {control.data?.paused
                ? t("Очередь приостановлена", "Queue paused")
                : t("Очередь активна", "Queue active")}
            </span>
            <button
              className="locale"
              onClick={() => setLocale(ru ? "en" : "ru")}
            >
              {locale.toUpperCase()}
            </button>
            <div className="avatar small">
              {me.data?.username.slice(0, 1).toUpperCase()}
            </div>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                {section === "dashboard"
                  ? "OPERATIONS OVERVIEW"
                  : "INFRASTRUCTURE CONTROL"}
              </div>
              <h1>
                {ru ? title?.ru : title?.en}
                <span className="heading-dot" />
              </h1>
              <p>
                {section === "dashboard"
                  ? t(
                      "Вся инфраструктура. Один центр управления.",
                      "Your entire infrastructure. One control center.",
                    )
                  : t(
                      "Реальные данные и управляемые операции вашей инфраструктуры.",
                      "Recorded data and controlled operations across your infrastructure.",
                    )}
              </p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void refresh()}
            >
              <RefreshCw size={14} />
              {t("Обновить", "Refresh")}
            </Button>
          </div>
          {notice && (
            <div
              role={notice.error ? "alert" : "status"}
              className={`notice ${notice.error ? "notice-error" : ""}`}
            >
              <span>
                {notice.error ? (
                  <AlertTriangle size={17} />
                ) : (
                  <CheckCircle2 size={17} />
                )}
              </span>
              <p>{notice.message}</p>
              <button aria-label="Dismiss" onClick={() => setNotice(null)}>
                <X size={16} />
              </button>
            </div>
          )}
          {canWrite && (
            <div className="command-bar">
              <div>
                <span className="command-indicator" />
                <strong>{t("Управление сессиями", "Session controls")}</strong>
                <small>
                  {selection.length
                    ? t(
                        `Выбрано аккаунтов: ${selection.length}`,
                        `${selection.length} accounts selected`,
                      )
                    : t(
                        "Выберите аккаунты в реестре",
                        "Select accounts in the registry",
                      )}
                </small>
              </div>
              <div className="command-buttons">
                <Button
                  size="sm"
                  onClick={() => {
                    if (!selection.length) {
                      setSection("accounts");
                      notify(
                        t(
                          "Выберите аккаунты флажками в таблице.",
                          "Select accounts using the table checkboxes.",
                        ),
                      );
                      return;
                    }
                    const batchKey = crypto.randomUUID();
                    setForm({
                      title: t(
                        "Запустить выбранные аккаунты",
                        "Start selected accounts",
                      ),
                      description: t(
                        `Заданий: ${selection.length}. Узлы распределяются планировщиком. Steam требует ручного входа.`,
                        `${selection.length} jobs. Scheduler assigns available nodes. Steam sign-in remains manual.`,
                      ),
                      fields: jobFields([], items(nodes.data), ru).filter(
                        (f) => f.name !== "account_id",
                      ),
                      submit: async (v) => {
                        const results = await Promise.allSettled(
                          selection.map((account_id) =>
                            api("/jobs", {
                              method: "POST",
                              headers: {
                                "Idempotency-Key": `${batchKey}:${account_id}`,
                              },
                              body: JSON.stringify({
                                ...jobBody(v),
                                account_id,
                              }),
                            }),
                          ),
                        );
                        const errors = results.flatMap((r, i) =>
                          r.status === "rejected"
                            ? [
                                `${selection[i]}: ${r.reason instanceof Error ? r.reason.message : String(r.reason)}`,
                              ]
                            : [],
                        );
                        if (errors.length)
                          throw new Error(
                            `${results.length - errors.length}/${results.length} queued. ${errors.join("; ")}`,
                          );
                        return { queued: results.length };
                      },
                    });
                  }}
                >
                  <Play size={13} />
                  {t("Запустить выбранные", "Start Selected")}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    if (!selection.length && !sessionSelection.length) {
                      setSection("sessions");
                      notify(
                        t(
                          "Выберите аккаунты или активные сессии в таблице.",
                          "Select accounts or active sessions in the table.",
                        ),
                      );
                      return;
                    }
                    confirm(
                      t(
                        "Остановить выбранные сессии",
                        "Stop selected sessions",
                      ),
                      `${selection.length} ${t("аккаунтов", "accounts")} · ${sessionSelection.length} ${t("сессий", "sessions")}`,
                      async () => {
                        const active = selection.length
                          ? (
                              await Promise.all(
                                ACTIVE.map((status) =>
                                  allPages(`/sessions?status=${status}`),
                                ),
                              )
                            ).flatMap((p) => p.items)
                          : [];
                        const ids = Array.from(
                          new Set([
                            ...sessionSelection,
                            ...active
                              .filter((s) =>
                                selection.includes(text(s.account_id)),
                              )
                              .map((s) => s.id),
                          ]),
                        );
                        if (!ids.length)
                          throw new Error(
                            t(
                              "У выбранных аккаунтов нет активных сессий.",
                              "Selected accounts have no active sessions.",
                            ),
                          );
                        const r = await Promise.allSettled(
                          ids.map((id) =>
                            api(`/sessions/${id}/stop`, { method: "POST" }),
                          ),
                        );
                        const errors = r.filter((x) => x.status === "rejected");
                        if (errors.length)
                          throw new Error(
                            `${errors.length} ${t("ошибок остановки. Обновите состояние.", "stop failures. Refresh state.")}`,
                          );
                        return { stopped: r.length };
                      },
                    );
                  }}
                >
                  <Square size={13} />
                  {t("Остановить выбранные", "Stop Selected")}
                </Button>
                <span className="command-divider" />
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() =>
                    confirm(
                      t("Аварийная остановка", "Emergency stop"),
                      t(
                        "Очередь будет приостановлена, активным управляемым сессиям отправлена остановка. Подтвердите действие.",
                        "Pause the queue and send stop commands to every active managed session. Confirm this operation.",
                      ),
                      () => api("/control/emergency-stop", { method: "POST" }),
                      true,
                    )
                  }
                >
                  <OctagonX size={14} />
                  {t("Аварийная остановка", "Emergency Stop")}
                </Button>
              </div>
            </div>
          )}
          {section === "dashboard" && <ProtectionSummary ru={ru} onOpen={() => setSection("protection")} />}
          {section === "protection" && <Protection ru={ru} role={me.data?.role || "viewer"} />}
          {section === "dashboard" && (
            <>
              {overview.error && (
                <div className="error-banner">{overview.error.message}</div>
              )}
              <div className="metric-grid">
                {metric(
                  t("Всего аккаунтов", "Total accounts"),
                  overviewData.accounts,
                  <Users size={18} />,
                  t(
                    `Активных: ${overviewData.active_accounts ?? "—"}`,
                    `Active: ${overviewData.active_accounts ?? "—"}`,
                  ),
                )}
                {metric(
                  t("Компьютеры онлайн", "Nodes online"),
                  overviewData.nodes_online,
                  <Monitor size={18} />,
                  t(
                    `Офлайн: ${overviewData.nodes_offline ?? "—"}`,
                    `Offline: ${overviewData.nodes_offline ?? "—"}`,
                  ),
                )}
                {metric(
                  t("Активные сессии", "Running sessions"),
                  overviewData.running_sessions,
                  <Play size={18} />,
                  t(
                    "Разрешённые управляемые сессии",
                    "Permitted managed sessions",
                  ),
                )}
                {metric(
                  t("Стоимость инвентарей", "Inventory value"),
                  overview.data ? money(overviewData.inventory_value) : null,
                  <Package size={18} />,
                  t(
                    "Оценка · USD · не денежный баланс",
                    "Estimate · USD · not a cash balance",
                  ),
                )}
              </div>
              <div className="dashboard-grid">
                <section className="panel chart-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>{t("Стоимость портфеля", "Portfolio value")}</h2>
                      <p>
                        {t(
                          "История сохранённых оценок инвентарей",
                          "Recorded inventory valuation history",
                        )}
                      </p>
                    </div>
                    <span className="subtle-badge">USD</span>
                  </div>
                  <Chart
                    data={
                      Array.isArray(overviewData.value_history)
                        ? overviewData.value_history
                        : []
                    }
                  />
                </section>
                <section className="panel resources-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>{t("Ресурсы узлов", "Node resources")}</h2>
                      <p>
                        {t(
                          "Средние по доступной телеметрии",
                          "Average available telemetry",
                        )}
                      </p>
                    </div>
                    <Activity size={17} />
                  </div>
                  {resource("CPU", overviewData.cpu_percent, "#58cda3")}
                  {resource("RAM", overviewData.ram_percent, "#6c9fe8")}
                  {resource("GPU", overviewData.gpu_percent, "#ab8dda")}
                  <div className="resource-foot">
                    <span className="connection-dot online" />
                    {t(
                      "Только реальные метрики агентов",
                      "Reported agent metrics only",
                    )}
                  </div>
                </section>
              </div>
              <div className="metric-grid compact">
                {metric(
                  t("Подтверждённые награды", "Confirmed drops"),
                  overviewData.confirmed_drops,
                  <Gift size={18} />,
                )}
                {metric(
                  t("Ошибки за 24 часа", "Errors in 24 hours"),
                  overviewData.errors_24h,
                  <AlertTriangle size={18} />,
                )}
                {metric(
                  t("Расходы", "Expenses"),
                  analytics.data ? money(analytics.data.expenses) : null,
                  <ShoppingCart size={18} />,
                )}
                {metric(
                  t("Реализованная прибыль", "Realized profit"),
                  analytics.data ? money(analytics.data.realized_profit) : null,
                  <ChartNoAxesCombined size={18} />,
                )}
              </div>
              <div className="dashboard-grid">
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <h2>{t("Задания", "Job activity")}</h2>
                      <p>
                        {t(
                          "Фактическое состояние очереди",
                          "Actual queue state",
                        )}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSection("scheduler")}
                    >
                      {t("Все задания", "All jobs")}
                      <ArrowUpRight size={14} />
                    </Button>
                  </div>
                  <div className="job-distribution">
                    {Object.entries(
                      (overviewData.job_counts || {}) as Record<string, number>,
                    ).length ? (
                      Object.entries(
                        (overviewData.job_counts || {}) as Record<
                          string,
                          number
                        >,
                      ).map(([status, count]) => (
                        <div key={status}>
                          <Status value={status} />
                          <strong>{count}</strong>
                        </div>
                      ))
                    ) : (
                      <div className="empty-inline">
                        {t("Задания ещё не создавались", "No jobs created yet")}
                      </div>
                    )}
                  </div>
                  {canWrite && (
                    <div className="panel-bottom">
                      <Button variant="secondary" onClick={createJob}>
                        <Plus size={14} />
                        {t("Создать задание", "Create job")}
                      </Button>
                    </div>
                  )}
                </section>
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <h2>{t("Подключённые компьютеры", "Connected nodes")}</h2>
                      <p>
                        {t(
                          "Последнее известное состояние",
                          "Last reported state",
                        )}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setSection("nodes")}
                    >
                      {t("Управление", "Manage")}
                      <ArrowUpRight size={14} />
                    </Button>
                  </div>
                  <div className="node-preview">
                    {items(nodes.data).length ? (
                      items(nodes.data)
                        .slice(0, 5)
                        .map((n) => (
                          <div key={n.id}>
                            <div className="node-icon">
                              <Monitor size={17} />
                            </div>
                            <div>
                              <strong>{text(n.hostname)}</strong>
                              <small>{text(n.os_version)}</small>
                            </div>
                            <Status value={n.status} />
                          </div>
                        ))
                    ) : (
                      <div className="empty-inline">
                        {t(
                          "Подключите первый Windows Agent",
                          "Enroll your first Windows Agent",
                        )}
                      </div>
                    )}
                  </div>
                </section>
              </div>
            </>
          )}
          {section === "accounts" && (
            <>
              <div className="section-info">
                <p>
                  {t(
                    "Prime и недельные награды подтверждаются явно. Пароли Steam не запрашиваются.",
                    "Prime status and weekly rewards require explicit confirmation. Steam passwords are never requested.",
                  )}
                </p>
                <Button variant="secondary" size="sm" onClick={() => setSection("protection")}>{t("Защита аккаунтов", "Account protection")}</Button>
              </div>
              <div className="filter-row">
                <label className="field">
                  <span>{t("Архив", "Archive")}</span>
                  <select
                    value={accountFilters.archived}
                    onChange={(e) => {
                      setPage(1);
                      setAccountFilters({
                        ...accountFilters,
                        archived: e.target.value,
                      });
                    }}
                  >
                    <option value="false">
                      {t("Активные записи", "Active records")}
                    </option>
                    <option value="true">
                      {t("Архивные записи", "Archived records")}
                    </option>
                  </select>
                </label>
                <label className="field">
                  <span>Prime</span>
                  <select
                    value={accountFilters.prime_status}
                    onChange={(e) => {
                      setPage(1);
                      setAccountFilters({
                        ...accountFilters,
                        prime_status: e.target.value,
                      });
                    }}
                  >
                    <option value="">{t("Все статусы", "All statuses")}</option>
                    {["unknown", "confirmed", "unconfirmed"].map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>{t("Компьютер", "Node")}</span>
                  <select
                    value={accountFilters.node_id}
                    onChange={(e) => {
                      setPage(1);
                      setAccountFilters({
                        ...accountFilters,
                        node_id: e.target.value,
                      });
                    }}
                  >
                    <option value="">{t("Все узлы", "All nodes")}</option>
                    {nodeOptions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <DataTable
                {...tableProps}
                selected={selection}
                onSelect={setSelection}
                actions={
                  <>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() =>
                        void exportFile("/accounts/export", "accounts.csv")
                      }
                    >
                      <Download size={14} />
                      {t("Экспорт", "Export")}
                    </Button>
                    {canWrite && (
                      <>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => fileInput.current?.click()}
                        >
                          <Upload size={14} />
                          CSV
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          disabled={!selection.length}
                          onClick={() =>
                            setForm({
                              title: t(
                                "Массовое редактирование",
                                "Bulk edit accounts",
                              ),
                              description: `${selection.length} ${t("аккаунтов", "accounts")}`,
                              fields: [
                                {
                                  name: "assigned_node_id",
                                  label: t(
                                    "Назначенный компьютер",
                                    "Assigned node",
                                  ),
                                  type: "select",
                                  options: nodeOptions,
                                },
                                {
                                  name: "group_id",
                                  label: t("Группа", "Group"),
                                  type: "select",
                                  options: (groups.data?.items || []).map(
                                    (g) => ({
                                      value: g.id,
                                      label: text(g.name),
                                    }),
                                  ),
                                },
                                {
                                  name: "tags",
                                  label: t(
                                    "Теги через запятую",
                                    "Comma-separated tags",
                                  ),
                                },
                                {
                                  name: "notes",
                                  label: t("Комментарий", "Notes"),
                                  type: "textarea",
                                },
                              ],
                              submit: (v) =>
                                api("/accounts/bulk", {
                                  method: "POST",
                                  body: JSON.stringify({
                                    ids: selection,
                                    changes: {
                                      ...(v.group_id
                                        ? { group_id: v.group_id }
                                        : {}),
                                      ...(v.assigned_node_id
                                        ? {
                                            assigned_node_id:
                                              v.assigned_node_id,
                                          }
                                        : {}),
                                      ...(v.tags
                                        ? {
                                            tags: text(v.tags)
                                              .split(",")
                                              .map((x) => x.trim())
                                              .filter(Boolean),
                                          }
                                        : {}),
                                      ...(v.notes ? { notes: v.notes } : {}),
                                    },
                                  }),
                                }),
                            })
                          }
                        >
                          {t("Изменить выбранные", "Bulk edit")}
                        </Button>
                        <Button size="sm" onClick={() => createAccount()}>
                          <Plus size={14} />
                          {t("Добавить", "Add account")}
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() =>
                            setForm({
                              title: t("Создать группу", "Create group"),
                              fields: [
                                {
                                  name: "name",
                                  label: t("Название", "Name"),
                                  required: true,
                                },
                              ],
                              submit: (v) =>
                                api("/groups", {
                                  method: "POST",
                                  body: JSON.stringify(v),
                                }),
                            })
                          }
                        >
                          {t("Новая группа", "New group")}
                        </Button>
                      </>
                    )}
                  </>
                }
                columns={[
                  {
                    key: "steam_id",
                    label: "ACCOUNT",
                    render: (r) => (
                      <div className="account-cell">
                        <div className="account-avatar">
                          {text(r.nickname || r.steam_id).slice(-2)}
                        </div>
                        <div>
                          <strong>{text(r.nickname || r.steam_id)}</strong>
                          <a
                            href={
                              text(r.profile_url) ||
                              `https://steamcommunity.com/profiles/${r.steam_id}`
                            }
                            target="_blank"
                            rel="noopener noreferrer"
                            className="mono"
                          >
                            {text(r.steam_id)}
                            <ExternalLink size={10} />
                          </a>
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: "prime_status",
                    label: "PRIME",
                    render: (r) => <Status value={r.prime_status} />,
                  },
                  {
                    key: "assigned_node_id",
                    label: t("КОМПЬЮТЕР", "NODE"),
                    render: (r) =>
                      text(
                        items(nodes.data).find(
                          (n) => n.id === r.assigned_node_id,
                        )?.hostname,
                      ) || "—",
                  },
                  {
                    key: "estimated_value",
                    label: t("ОЦЕНКА", "VALUE"),
                    render: (r) => money(r.estimated_value),
                  },
                  {
                    key: "group_id",
                    label: t("ГРУППА", "GROUP"),
                    render: (r) =>
                      text(
                        groups.data?.items.find((g) => g.id === r.group_id)
                          ?.name,
                      ) || "—",
                  },
                  {
                    key: "tags",
                    label: t("ТЕГИ", "TAGS"),
                    render: (r) =>
                      Array.isArray(r.tags) ? (
                        <div className="tags">
                          {r.tags.map((tag, i) => (
                            <span key={i}>{String(tag)}</span>
                          ))}
                        </div>
                      ) : (
                        "—"
                      ),
                  },
                  {
                    key: "last_sync",
                    label: t("СИНХРОНИЗАЦИЯ", "LAST SYNC"),
                    render: (r) => date(r.last_sync),
                  },
                  {
                    key: "actions",
                    label: "",
                    render: (r) => (
                      <div className="row-actions">
                        {actionButton(t("Инвентарь", "Inventory"), () => {
                          setAccountId(r.id);
                          setSection("inventory");
                        })}
                        {canWrite && (
                          <>
                            {actionButton(t("Изменить", "Edit"), () =>
                              createAccount(r),
                            )}
                            {actionButton(
                              t("Удалить", "Delete"),
                              () =>
                                confirm(
                                  t(
                                    "Удалить / архивировать аккаунт",
                                    "Delete / archive account",
                                  ),
                                  text(r.steam_id),
                                  () =>
                                    api(`/accounts/${r.id}`, {
                                      method: "DELETE",
                                    }),
                                  true,
                                ),
                              true,
                            )}
                          </>
                        )}
                      </div>
                    ),
                  },
                ]}
              />
              <input
                ref={fileInput}
                type="file"
                accept=".csv,text/csv"
                hidden
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  const contents = await f.text();
                  e.target.value = "";
                  setForm({
                    title: t("Импорт CSV", "Import CSV"),
                    description: `${f.name} · ${t("Заголовок: steam_id,notes,tags. Никаких паролей.", "Header: steam_id,notes,tags. No passwords.")}`,
                    fields: [],
                    submit: () =>
                      api("/accounts/import", {
                        method: "POST",
                        headers: { "Content-Type": "text/csv" },
                        body: contents,
                      }),
                  });
                }}
              />
            </>
          )}
          {section === "nodes" && (
            <>
              <div className="section-info">
                <p>
                  {t(
                    "Скачайте установщик агента, запустите мастер на другом ПК и введите адрес доверенного HTTPS-сервера и одноразовый код. Адрес localhost не подходит для другого компьютера. Запуск Steam требует интерактивного рабочего стола Windows.",
                    "Download the agent installer, run its wizard on another PC, and enter the trusted HTTPS server address and a one-time code. Localhost cannot connect another computer. Steam requires an interactive Windows desktop.",
                  )}
                </p>
              </div>
              <AgentInstaller ru={ru} />
              <DataTable
                {...tableProps}
                actions={
                  me.data?.role === "admin" && (
                    <Button
                      size="sm"
                      onClick={() =>
                        confirm(
                          t(
                            "Создать токен привязки",
                            "Create enrollment token",
                          ),
                          t(
                            "Токен будет показан один раз. Используйте его при установке агента.",
                            "The token is shown once. Use it during agent setup.",
                          ),
                          async () => {
                            const r = await api<{
                              token: string;
                              expires_at: string;
                            }>("/nodes/enrollment-tokens", { method: "POST" });
                            setSecret(`${r.token}\nExpires: ${r.expires_at}`);
                            return r;
                          },
                        )
                      }
                    >
                      <Plus size={14} />
                      {t("Подключить компьютер", "Enroll node")}
                    </Button>
                  )
                }
                columns={[
                  {
                    key: "hostname",
                    label: "NODE",
                    render: (r) => (
                      <div className="account-cell">
                        <div className="node-icon">
                          <Monitor size={18} />
                        </div>
                        <div>
                          <strong>{text(r.hostname)}</strong>
                          <small className="mono">{r.id}</small>
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: "status",
                    label: t("СОСТОЯНИЕ", "STATUS"),
                    render: (r) => <Status value={r.status} />,
                  },
                  { key: "os_version", label: "OS" },
                  {
                    key: "metrics",
                    label: "CPU / RAM",
                    render: (r) => {
                      const m = (r.metrics || {}) as Row;
                      return `${m.cpu_percent ?? "—"}% / ${m.ram_percent ?? "—"}%`;
                    },
                  },
                  { key: "agent_version", label: "AGENT" },
                  {
                    key: "last_heartbeat",
                    label: t("ПОСЛЕДНИЙ ОТВЕТ", "LAST HEARTBEAT"),
                    render: (r) => date(r.last_heartbeat),
                  },
                  {
                    key: "actions",
                    label: "",
                    render: (r) => (
                      <div className="row-actions">
                        {actionButton(t("Диагностика", "Details"), async () => {
                          const metrics = await api(
                            `/nodes/${r.id}/metrics`,
                          ).catch((e) => ({ error: e.message }));
                          setSecret(
                            JSON.stringify(
                              { ...r, metric_history: metrics },
                              null,
                              2,
                            ),
                          );
                        })}
                        {canWrite && (
                          <>
                            {actionButton(t("Перезапуск", "Restart"), () =>
                              confirm(
                                t("Перезапустить агент", "Restart agent"),
                                text(r.hostname),
                                () =>
                                  api(`/nodes/${r.id}/restart`, {
                                    method: "POST",
                                  }),
                              ),
                            )}
                            {actionButton(
                              t("Отозвать", "Revoke"),
                              () =>
                                confirm(
                                  t(
                                    "Отозвать доступ агента",
                                    "Revoke agent access",
                                  ),
                                  text(r.hostname),
                                  () =>
                                    api(`/nodes/${r.id}/revoke`, {
                                      method: "POST",
                                    }),
                                  true,
                                ),
                              true,
                            )}
                          </>
                        )}
                      </div>
                    ),
                  },
                ]}
              />
            </>
          )}
          {section === "sessions" && (
            <DataTable
              {...tableProps}
              selected={sessionSelection}
              onSelect={setSessionSelection}
              actions={
                canWrite && (
                  <Button size="sm" onClick={createJob}>
                    <Plus size={14} />
                    {t("Создать задание", "Create job")}
                  </Button>
                )
              }
              columns={[
                {
                  key: "id",
                  label: "SESSION",
                  render: (r) => (
                    <span className="mono" title={r.id}>
                      {r.id.slice(0, 8)}
                    </span>
                  ),
                },
                {
                  key: "account_id",
                  label: "ACCOUNT",
                  render: (r) =>
                    text(
                      items(accounts.data).find((a) => a.id === r.account_id)
                        ?.steam_id,
                    ) || "—",
                },
                {
                  key: "node_id",
                  label: "NODE",
                  render: (r) =>
                    text(
                      items(nodes.data).find((n) => n.id === r.node_id)
                        ?.hostname,
                    ) || text(r.node_id),
                },
                {
                  key: "status",
                  label: "STATUS",
                  render: (r) => <Status value={r.status} />,
                },
                { key: "pid", label: "PID" },
                {
                  key: "started_at",
                  label: "STARTED",
                  render: (r) => date(r.started_at),
                },
                {
                  key: "error",
                  label: t("ДИАГНОСТИКА", "DIAGNOSTICS"),
                  render: (r) => (
                    <span className="truncate" title={text(r.error)}>
                      {text(r.error) || `Exit: ${r.exit_code ?? "—"}`}
                    </span>
                  ),
                },
                {
                  key: "actions",
                  label: "",
                  render: (r) =>
                    canWrite && (
                      <div className="row-actions">
                        {ACTIVE.includes(text(r.status)) &&
                          actionButton(
                            t("Остановить", "Stop"),
                            () =>
                              confirm(
                                t("Остановить сессию", "Stop session"),
                                r.id,
                                () =>
                                  api(`/sessions/${r.id}/stop`, {
                                    method: "POST",
                                  }),
                              ),
                            true,
                          )}
                        {actionButton(t("Перезапуск", "Restart"), () =>
                          confirm(
                            t("Перезапустить сессию", "Restart session"),
                            r.id,
                            () =>
                              api(`/sessions/${r.id}/restart`, {
                                method: "POST",
                              }),
                          ),
                        )}
                      </div>
                    ),
                },
              ]}
            />
          )}
          {section === "scheduler" && (
            <>
              <DataTable
                {...tableProps}
                actions={
                  canWrite && (
                    <>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() =>
                          confirm(
                            control.data?.paused
                              ? t("Возобновить очередь", "Resume queue")
                              : t("Приостановить очередь", "Pause queue"),
                            t(
                              "Действие изменяет выдачу новых заданий.",
                              "This controls dispatch of new jobs.",
                            ),
                            () =>
                              api(
                                `/control/${control.data?.paused ? "resume" : "pause"}`,
                                { method: "POST" },
                              ),
                          )
                        }
                      >
                        {control.data?.paused ? (
                          <Play size={14} />
                        ) : (
                          <Pause size={14} />
                        )}
                        {control.data?.paused
                          ? t("Возобновить", "Resume")
                          : t("Пауза", "Pause")}
                      </Button>
                      <Button size="sm" onClick={createJob}>
                        <Plus size={14} />
                        {t("Новое задание", "New job")}
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() =>
                          setForm({
                            title: t("Политика очереди", "Queue policy"),
                            fields: [
                              {
                                name: "policy",
                                label: t("Политика", "Policy"),
                                type: "select",
                                required: true,
                                value: control.data?.policy || "fifo",
                                options: [
                                  "fifo",
                                  "priority",
                                  "round_robin",
                                ].map((value) => ({ value, label: value })),
                              },
                            ],
                            submit: (v) =>
                              api("/control", {
                                method: "PATCH",
                                body: JSON.stringify(v),
                              }),
                          })
                        }
                      >
                        {text(control.data?.policy || "fifo")}
                      </Button>
                    </>
                  )
                }
                columns={[
                  {
                    key: "id",
                    label: "JOB",
                    render: (r) => (
                      <div>
                        <strong className="mono">{r.id.slice(0, 8)}</strong>
                        <small>{text(r.kind)}</small>
                      </div>
                    ),
                  },
                  {
                    key: "status",
                    label: "STATUS",
                    render: (r) => <Status value={r.status} />,
                  },
                  {
                    key: "node_id",
                    label: "NODE",
                    render: (r) =>
                      text(
                        items(nodes.data).find((n) => n.id === r.node_id)
                          ?.hostname,
                      ) || t("Авто", "Auto"),
                  },
                  { key: "priority", label: t("ПРИОРИТЕТ", "PRIORITY") },
                  {
                    key: "attempts",
                    label: t("ПОПЫТКИ", "ATTEMPTS"),
                    render: (r) => `${r.attempts ?? 0} / ${r.max_attempts}`,
                  },
                  {
                    key: "scheduled_at",
                    label: t("ПЛАНОВОЕ ВРЕМЯ", "SCHEDULED"),
                    render: (r) => date(r.scheduled_at),
                  },
                  {
                    key: "error",
                    label: t("ДИАГНОСТИКА", "DIAGNOSTICS"),
                    render: (r) => (
                      <span className="truncate" title={text(r.error)}>
                        {display(r.error)}
                      </span>
                    ),
                  },
                  {
                    key: "actions",
                    label: "",
                    render: (r) => (
                      <div className="row-actions">
                        {actionButton(t("Подробности", "Details"), () =>
                          setSecret(JSON.stringify(r, null, 2)),
                        )}
                        {canWrite &&
                          (["FAILED", "TIMED_OUT", "CANCELLED"].includes(
                            text(r.status),
                          )
                            ? actionButton(t("Повторить", "Retry"), () =>
                                confirm(
                                  t("Повторить задание", "Retry job"),
                                  r.id,
                                  () =>
                                    api(`/jobs/${r.id}/retry`, {
                                      method: "POST",
                                    }),
                                ),
                              )
                            : ACTIVE.includes(text(r.status)) &&
                              actionButton(
                                t("Отменить", "Cancel"),
                                () =>
                                  confirm(
                                    t("Отменить задание", "Cancel job"),
                                    r.id,
                                    () =>
                                      api(`/jobs/${r.id}/cancel`, {
                                        method: "POST",
                                      }),
                                  ),
                                true,
                              ))}
                      </div>
                    ),
                  },
                ]}
              />
              <div className="subsection-heading">
                <div>
                  <h2>{t("Повторяющиеся задания", "Recurring schedules")}</h2>
                  <p>
                    {t(
                      "Интервалы в секундах · временные метки UTC",
                      "Intervals in seconds · UTC timestamps",
                    )}
                  </p>
                </div>
                {canWrite && (
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() =>
                      setForm({
                        title: t("Создать расписание", "Create schedule"),
                        fields: [
                          {
                            name: "name",
                            label: t("Название", "Name"),
                            required: true,
                          },
                          {
                            name: "interval_seconds",
                            label: t("Интервал, с", "Interval, seconds"),
                            type: "number",
                            required: true,
                            value: 3600,
                            min: 60,
                          },
                          {
                            name: "next_run",
                            label: t(
                              "Первый запуск (местное время)",
                              "First run (local time)",
                            ),
                            type: "datetime-local",
                            required: true,
                          },
                          ...jobFields(
                            items(accounts.data),
                            items(nodes.data),
                            ru,
                          ),
                        ],
                        submit: (v) =>
                          api("/schedules", {
                            method: "POST",
                            body: JSON.stringify({
                              name: v.name,
                              interval_seconds: v.interval_seconds,
                              next_run: new Date(
                                text(v.next_run),
                              ).toISOString(),
                              job_template: jobBody(v),
                            }),
                          }),
                      })
                    }
                  >
                    <Plus size={14} />
                    {t("Добавить расписание", "Add schedule")}
                  </Button>
                )}
              </div>
              <DataTable
                data={scheduleList.data}
                loading={scheduleList.isPending}
                error={scheduleList.error}
                page={1}
                onPage={undefined}
                columns={[
                  { key: "name", label: t("НАЗВАНИЕ", "NAME") },
                  { key: "enabled", label: t("АКТИВНО", "ENABLED") },
                  {
                    key: "interval_seconds",
                    label: t("ИНТЕРВАЛ, С", "INTERVAL, S"),
                  },
                  {
                    key: "next_run",
                    label: t("СЛЕДУЮЩИЙ ЗАПУСК", "NEXT RUN"),
                    render: (r) => date(r.next_run),
                  },
                  {
                    key: "actions",
                    label: "",
                    render: (r) =>
                      canWrite && (
                        <div className="row-actions">
                          {actionButton(
                            r.enabled
                              ? t("Пауза", "Pause")
                              : t("Включить", "Enable"),
                            () =>
                              confirm(
                                t("Изменить расписание", "Update schedule"),
                                text(r.name),
                                () =>
                                  api(`/schedules/${r.id}`, {
                                    method: "PATCH",
                                    body: JSON.stringify({
                                      enabled: !r.enabled,
                                    }),
                                  }),
                              ),
                          )}
                          {actionButton(
                            t("Удалить", "Delete"),
                            () =>
                              confirm(
                                t("Удалить расписание", "Delete schedule"),
                                text(r.name),
                                () =>
                                  api(`/schedules/${r.id}`, {
                                    method: "DELETE",
                                  }),
                                true,
                              ),
                            true,
                          )}
                        </div>
                      ),
                  },
                ]}
              />
            </>
          )}
          {section === "drops" && (
            <>
              <div className="section-info">
                <p>
                  {t(
                    "Новый предмет не доказывает получение Weekly Care Package. Подтверждайте награду вручную с указанием источника. Периоды задаются по проверенному актуальному источнику.",
                    "A new item does not establish receipt of a Weekly Care Package. Confirm rewards with an evidence source. Define periods using a current verified source.",
                  )}
                </p>
              </div>
              <DataTable
                {...tableProps}
                actions={
                  canWrite && (
                    <>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() =>
                          setForm({
                            title: t("Недельный период", "Weekly period"),
                            description: t(
                              "Укажите реальный интервал сброса CS2 и ссылку на актуальный источник.",
                              "Enter the actual CS2 reset interval and a current source URL.",
                            ),
                            fields: [
                              {
                                name: "starts_at",
                                label: t(
                                  "Начало (местное время)",
                                  "Start (local time)",
                                ),
                                type: "datetime-local",
                                required: true,
                              },
                              {
                                name: "ends_at",
                                label: t(
                                  "Конец (местное время)",
                                  "End (local time)",
                                ),
                                type: "datetime-local",
                                required: true,
                              },
                              {
                                name: "source",
                                label: t(
                                  "Источник подтверждения цикла",
                                  "Reset-cycle evidence source",
                                ),
                                required: true,
                              },
                            ],
                            submit: (v) =>
                              api("/drops/periods", {
                                method: "POST",
                                body: JSON.stringify({
                                  starts_at: new Date(
                                    text(v.starts_at),
                                  ).toISOString(),
                                  ends_at: new Date(
                                    text(v.ends_at),
                                  ).toISOString(),
                                  source: v.source,
                                }),
                              }),
                          })
                        }
                      >
                        <Plus size={14} />
                        {t("Добавить период", "Add period")}
                      </Button>
                      <Button
                        size="sm"
                        onClick={() =>
                          setForm({
                            title: t("Подтвердить награду", "Confirm drop"),
                            description: t(
                              "Сохраняется ручное подтверждение и его источник.",
                              "Stores a manual confirmation and its source.",
                            ),
                            fields: [
                              {
                                name: "account_id",
                                label: t("Аккаунт", "Account"),
                                type: "select",
                                required: true,
                                options: accountOptions,
                              },
                              {
                                name: "period_id",
                                label: t("Период", "Period"),
                                type: "select",
                                required: true,
                                options: items(periods.data).map((p) => ({
                                  value: p.id,
                                  label: `${date(p.starts_at)} — ${date(p.ends_at)}`,
                                })),
                              },
                              {
                                name: "item_name",
                                label: t("Название предмета", "Item name"),
                                required: true,
                              },
                              {
                                name: "value",
                                label: t(
                                  "Оценочная стоимость, USD",
                                  "Estimated value, USD",
                                ),
                                type: "number",
                                step: "0.01",
                                min: 0,
                                required: true,
                              },
                              {
                                name: "source",
                                label: t(
                                  "Источник подтверждения",
                                  "Confirmation source",
                                ),
                                required: true,
                              },
                            ],
                            submit: (v) =>
                              api("/drops/confirm", {
                                method: "POST",
                                body: JSON.stringify({ ...v, currency: "USD" }),
                              }),
                          })
                        }
                      >
                        <Gift size={14} />
                        {t("Подтвердить награду", "Confirm reward")}
                      </Button>
                    </>
                  )
                }
                columns={[
                  {
                    key: "account_id",
                    label: "ACCOUNT",
                    render: (r) =>
                      text(
                        items(accounts.data).find((a) => a.id === r.account_id)
                          ?.steam_id,
                      ) || text(r.account_id),
                  },
                  {
                    key: "period_id",
                    label: t("ПЕРИОД", "PERIOD"),
                    render: (r) => {
                      const p = items(periods.data).find(
                        (p) => p.id === r.period_id,
                      );
                      return p ? date(p.starts_at) : text(r.period_id);
                    },
                  },
                  {
                    key: "status",
                    label: "STATUS",
                    render: (r) => <Status value={r.status} />,
                  },
                  { key: "item_name", label: t("ПРЕДМЕТ", "ITEM") },
                  { key: "value", label: "USD", render: (r) => money(r.value) },
                  { key: "source", label: t("ИСТОЧНИК", "SOURCE") },
                  {
                    key: "confirmed_at",
                    label: t("ПОДТВЕРЖДЕНО", "CONFIRMED"),
                    render: (r) => date(r.confirmed_at),
                  },
                ]}
              />
              <section className="panel period-panel">
                <div className="panel-heading">
                  <h2>
                    {t("Зарегистрированные периоды", "Registered periods")}
                  </h2>
                </div>
                {periods.error && (
                  <div className="error-banner">{periods.error.message}</div>
                )}
                {items(periods.data).length ? (
                  <div className="period-list">
                    {items(periods.data).map((p) => (
                      <div key={p.id}>
                        <strong>
                          {date(p.starts_at)} → {date(p.ends_at)}
                        </strong>
                        <span>{text(p.source)}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="empty-inline">
                    {t(
                      "Периоды не зарегистрированы.",
                      "No periods registered.",
                    )}
                  </div>
                )}
              </section>
            </>
          )}
          {section === "inventory" && (
            <>
              <section className="panel inventory-toolbar">
                <label className="field">
                  <span>{t("Аккаунт", "Account")}</span>
                  <select
                    value={accountId}
                    onChange={(e) => setAccountId(e.target.value)}
                  >
                    <option value="">
                      {t("Выберите аккаунт", "Select account")}
                    </option>
                    {accountOptions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="toolbar-actions">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={!accountId}
                    onClick={() =>
                      void exportFile(
                        `/inventory/export?account_id=${accountId}`,
                        "inventory.csv",
                      )
                    }
                  >
                    <Download size={14} />
                    CSV
                  </Button>
                  {canWrite && (
                    <Button
                      size="sm"
                      disabled={!accountId}
                      onClick={syncInventory}
                    >
                      <RefreshCw size={14} />
                      {t("Синхронизировать", "Synchronize")}
                    </Button>
                  )}
                </div>
              </section>
              {!accountId ? (
                <div className="panel empty-state">
                  <Package size={38} />
                  <p>
                    {t(
                      "Выберите аккаунт для просмотра доступного инвентаря.",
                      "Select an account to view its available inventory.",
                    )}
                  </p>
                </div>
              ) : (
                <>
                  <DataTable
                    data={{
                      items: (inventory.data?.items || []).filter(
                        (r) =>
                          !inventoryQ ||
                          [r.name, r.market_hash_name, r.asset_id].some((v) =>
                            text(v)
                              .toLowerCase()
                              .includes(inventoryQ.toLowerCase()),
                          ),
                      ),
                      total: (inventory.data?.items || []).filter(
                        (r) =>
                          !inventoryQ ||
                          [r.name, r.market_hash_name, r.asset_id].some((v) =>
                            text(v)
                              .toLowerCase()
                              .includes(inventoryQ.toLowerCase()),
                          ),
                      ).length,
                      page: 1,
                      page_size: Math.max(1, inventory.data?.items.length || 0),
                    }}
                    loading={inventory.isPending}
                    error={inventory.error}
                    search={inventoryQ}
                    onSearch={setInventoryQ}
                    page={1}
                    onPage={undefined}
                    columns={[
                      {
                        key: "name",
                        label: t("ПРЕДМЕТ", "ITEM"),
                        render: (r) =>
                          text(r.name || r.market_hash_name || r.asset_id),
                      },
                      { key: "asset_id", label: "ASSET ID" },
                      { key: "type", label: t("ТИП", "TYPE") },
                      { key: "amount", label: t("КОЛИЧЕСТВО", "AMOUNT") },
                      { key: "market_hash_name", label: "MARKET NAME" },
                      {
                        key: "estimated_value",
                        label: "USD",
                        render: (r) => money(r.estimated_value ?? r.price),
                      },
                      { key: "tradable", label: "TRADABLE" },
                    ]}
                    empty={t(
                      "Снимков ещё нет либо инвентарь пуст. Запустите синхронизацию.",
                      "No snapshots yet or the inventory is empty. Start a sync.",
                    )}
                  />
                  <div className="dashboard-grid">
                    <section className="panel">
                      <div className="panel-heading">
                        <h2>{t("Снимки инвентаря", "Inventory snapshots")}</h2>
                      </div>
                      <div className="period-list">
                        {inventory.data?.snapshots.length ? (
                          inventory.data.snapshots.map((s) => (
                            <div key={s.id}>
                              <strong>
                                {date(s.fetched_at || s.created_at)}
                              </strong>
                              <span>
                                {text(s.status)} · {text(s.item_count ?? "—")}{" "}
                                {t("предметов", "items")}
                              </span>
                              {!!s.error && (
                                <small className="error-text">
                                  {text(s.error)}
                                </small>
                              )}
                            </div>
                          ))
                        ) : (
                          <div className="empty-inline">
                            {t("Нет снимков", "No snapshots")}
                          </div>
                        )}
                      </div>
                    </section>
                    <section className="panel">
                      <div className="panel-heading">
                        <h2>{t("Изменения", "Changes")}</h2>
                      </div>
                      <div className="period-list">
                        {inventory.data?.changes.length ? (
                          inventory.data.changes.slice(0, 30).map((c) => (
                            <div key={c.id}>
                              <strong>
                                {text(c.change_type || c.kind)} ·{" "}
                                {text(c.asset_id)}
                              </strong>
                              <span>{date(c.detected_at || c.created_at)}</span>
                            </div>
                          ))
                        ) : (
                          <div className="empty-inline">
                            {t("Изменений нет", "No changes")}
                          </div>
                        )}
                      </div>
                    </section>
                  </div>
                </>
              )}
            </>
          )}
          {section === "market" && (
            <>
              <div className="section-info">
                <p>
                  {t(
                    "Котировки Steam Community Market могут быть недоступны. Цена листинга не равна фактической выручке; Steam Wallet не является выводимыми деньгами.",
                    "Steam Community Market quotes may be unavailable. Listing prices differ from actual proceeds; Steam Wallet is not withdrawable cash.",
                  )}
                </p>
                <span className="subtle-badge">USD</span>
              </div>
              <DataTable
                {...tableProps}
                columns={[
                  { key: "market_hash_name", label: t("ПРЕДМЕТ", "ITEM") },
                  {
                    key: "price",
                    label: t("КОТИРОВКА", "QUOTE"),
                    render: (r) => money(r.price),
                  },
                  { key: "currency", label: t("ВАЛЮТА", "CURRENCY") },
                  { key: "source", label: t("ИСТОЧНИК", "SOURCE") },
                  {
                    key: "fetched_at",
                    label: t("ПОЛУЧЕНО", "FETCHED"),
                    render: (r) => date(r.fetched_at || r.created_at),
                  },
                ]}
              />
            </>
          )}
          {section === "analytics" && (
            <>
              {analytics.error && (
                <div className="error-banner">{analytics.error.message}</div>
              )}
              <div className="metric-grid">
                {metric(
                  t("Фактические продажи", "Recorded sales"),
                  analytics.data ? money(analytics.data.revenue) : null,
                  <ShoppingCart size={18} />,
                )}
                {metric(
                  t("Расходы", "Expenses"),
                  analytics.data ? money(analytics.data.expenses) : null,
                  <Package size={18} />,
                )}
                {metric(
                  t("Реализованная прибыль", "Realized profit"),
                  analytics.data ? money(analytics.data.realized_profit) : null,
                  <ChartNoAxesCombined size={18} />,
                )}
                {metric(
                  "ROI",
                  analytics.data?.roi == null
                    ? null
                    : `${Number(analytics.data.roi).toFixed(2)}%`,
                  <ArrowUpRight size={18} />,
                )}
              </div>
              <div className="dashboard-grid">
                <section className="panel chart-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>
                        {t("Денежные операции", "Recorded financial activity")}
                      </h2>
                      <p>
                        {t(
                          "Записанные продажи и расходы по дням · USD",
                          "Recorded sales and expenses by day · USD",
                        )}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() =>
                        void exportFile("/analytics/export", "analytics.csv")
                      }
                    >
                      <Download size={14} />
                      {t("Отчёт", "Report")}
                    </Button>
                  </div>
                  {Array.isArray(analytics.data?.series) &&
                  analytics.data.series.length ? (
                    <div className="chart">
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={analytics.data.series}>
                          <CartesianGrid
                            stroke="#26302f"
                            strokeDasharray="3 4"
                            vertical={false}
                          />
                          <XAxis
                            dataKey="date"
                            tick={{ fill: "#7f8a89", fontSize: 10 }}
                          />
                          <YAxis tick={{ fill: "#7f8a89", fontSize: 11 }} />
                          <Tooltip
                            contentStyle={{
                              background: "#19201f",
                              border: "1px solid #33403d",
                            }}
                          />
                          <Bar
                            dataKey="revenue"
                            fill="#51c598"
                            radius={[3, 3, 0, 0]}
                          />
                          <Bar
                            dataKey="expenses"
                            fill="#6b9cdd"
                            radius={[3, 3, 0, 0]}
                          />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  ) : (
                    <div className="empty-chart chart">
                      <p>
                        {t(
                          "Добавьте расходы или продажи",
                          "Record expenses or sales",
                        )}
                      </p>
                    </div>
                  )}
                </section>
                <section className="panel formulas">
                  <div className="panel-heading">
                    <h2>
                      {t("Прозрачные расчёты", "Transparent calculations")}
                    </h2>
                  </div>
                  <dl>
                    <dt>{t("Комиссии", "Fees")}</dt>
                    <dd>{money(analytics.data?.fees)}</dd>
                    <dt>
                      {t(
                        "Нереализованная оценка",
                        "Unrealized inventory estimate",
                      )}
                    </dt>
                    <dd>
                      {money(
                        analytics.data?.unrealized_value ??
                          analytics.data?.inventory_value,
                      )}
                    </dd>
                    <dt>{t("Окупаемость, месяцев", "Payback, months")}</dt>
                    <dd>
                      {analytics.data?.payback_months == null
                        ? "—"
                        : Number(analytics.data.payback_months).toFixed(1)}
                    </dd>
                  </dl>
                  <p>
                    {t(
                      "Прибыль = продажи − комиссии − расходы. ROI = прибыль / расходы × 100%. При нулевых расходах ROI не определён.",
                      "Profit = sales − fees − expenses. ROI = profit / expenses × 100%. ROI is undefined when expenses are zero.",
                    )}
                  </p>
                  <p>
                    {t(
                      "Оценка инвентаря не включена в реализованную прибыль.",
                      "Inventory valuation is excluded from realized profit.",
                    )}
                  </p>
                </section>
              </div>
              <div className="subsection-heading">
                <h2>{t("Расходы", "Expenses")}</h2>
                {canWrite && (
                  <Button
                    size="sm"
                    onClick={() =>
                      setForm({
                        title: t("Добавить расход", "Record expense"),
                        fields: [
                          {
                            name: "category",
                            label: t("Категория", "Category"),
                            type: "select",
                            required: true,
                            options: [
                              "prime",
                              "hardware",
                              "electricity",
                              "server",
                              "maintenance",
                              "other",
                            ].map((value) => ({ value, label: value })),
                          },
                          {
                            name: "amount",
                            label: "USD",
                            type: "number",
                            step: "0.01",
                            min: 0,
                            required: true,
                          },
                          {
                            name: "description",
                            label: t("Описание", "Description"),
                            required: true,
                          },
                          {
                            name: "occurred_at",
                            label: t(
                              "Дата (местное время)",
                              "Date (local time)",
                            ),
                            type: "datetime-local",
                            required: true,
                          },
                        ],
                        submit: (v) =>
                          api("/expenses", {
                            method: "POST",
                            body: JSON.stringify({
                              ...v,
                              currency: "USD",
                              occurred_at: new Date(
                                text(v.occurred_at),
                              ).toISOString(),
                            }),
                          }),
                      })
                    }
                  >
                    <Plus size={14} />
                    {t("Добавить расход", "Record expense")}
                  </Button>
                )}
              </div>
              <DataTable
                data={expenses.data}
                loading={expenses.isPending}
                error={expenses.error}
                page={page}
                onPage={setPage}
                columns={[
                  {
                    key: "occurred_at",
                    label: "DATE",
                    render: (r) => date(r.occurred_at),
                  },
                  { key: "category", label: "CATEGORY" },
                  { key: "description", label: "DESCRIPTION" },
                  {
                    key: "amount",
                    label: "USD",
                    render: (r) => money(r.amount),
                  },
                ]}
              />
              <div className="subsection-heading">
                <h2>{t("Фактические продажи", "Recorded sales")}</h2>
                {canWrite && (
                  <Button
                    size="sm"
                    onClick={() =>
                      setForm({
                        title: t("Записать продажу", "Record sale"),
                        fields: [
                          {
                            name: "amount",
                            label: t("Сумма продажи, USD", "Sale amount, USD"),
                            type: "number",
                            step: "0.01",
                            min: 0,
                            required: true,
                          },
                          {
                            name: "fees",
                            label: t("Комиссии, USD", "Fees, USD"),
                            type: "number",
                            step: "0.01",
                            min: 0,
                            required: true,
                            value: 0,
                          },
                          {
                            name: "description",
                            label: t("Описание", "Description"),
                            required: true,
                          },
                          {
                            name: "occurred_at",
                            label: t(
                              "Дата (местное время)",
                              "Date (local time)",
                            ),
                            type: "datetime-local",
                            required: true,
                          },
                        ],
                        submit: (v) =>
                          api("/revenues", {
                            method: "POST",
                            body: JSON.stringify({
                              ...v,
                              currency: "USD",
                              occurred_at: new Date(
                                text(v.occurred_at),
                              ).toISOString(),
                            }),
                          }),
                      })
                    }
                  >
                    <Plus size={14} />
                    {t("Записать продажу", "Record sale")}
                  </Button>
                )}
              </div>
              <DataTable
                data={revenues.data}
                loading={revenues.isPending}
                error={revenues.error}
                page={page}
                onPage={setPage}
                columns={[
                  {
                    key: "occurred_at",
                    label: "DATE",
                    render: (r) => date(r.occurred_at),
                  },
                  { key: "description", label: "DESCRIPTION" },
                  {
                    key: "amount",
                    label: "GROSS USD",
                    render: (r) => money(r.amount),
                  },
                  {
                    key: "fees",
                    label: "FEES USD",
                    render: (r) => money(r.fees),
                  },
                ]}
              />
            </>
          )}
          {section === "logs" && (
            <DataTable
              {...tableProps}
              columns={[
                {
                  key: "created_at",
                  label: "TIME",
                  render: (r) => date(r.created_at),
                },
                { key: "actor", label: "ACTOR" },
                {
                  key: "action",
                  label: "ACTION",
                  render: (r) => (
                    <span className="mono action-name">{text(r.action)}</span>
                  ),
                },
                {
                  key: "target",
                  label: "TARGET",
                  render: (r) => (
                    <span className="mono truncate" title={text(r.target)}>
                      {display(r.target)}
                    </span>
                  ),
                },
                {
                  key: "details",
                  label: "DETAILS",
                  render: (r) => (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        setSecret(JSON.stringify(r.details, null, 2))
                      }
                    >
                      {t("Просмотр", "View")}
                      <ArrowUpRight size={12} />
                    </Button>
                  ),
                },
              ]}
            />
          )}
          {section === "settings" && (
            <>
              <SystemManagement ru={ru} admin={me.data?.role === "admin"} />
              <section className="panel settings-panel">
                <div className="panel-heading">
                  <div>
                    <h2>{t("Настройки системы", "System configuration")}</h2>
                    <p>
                      {t(
                        "Конфигурация текущего сервера. Секреты не передаются в браузер.",
                        "Current server configuration. Secrets are never sent to the browser.",
                      )}
                    </p>
                  </div>
                  <ShieldCheck size={20} />
                </div>
                {settings.error ? (
                  <div className="error-banner">{settings.error.message}</div>
                ) : (
                  <dl>
                    {Object.entries(settings.data || {}).map(([key, value]) => (
                      <div key={key}>
                        <dt>{key.replaceAll("_", " ")}</dt>
                        <dd>{display(value)}</dd>
                      </div>
                    ))}
                  </dl>
                )}
                <div className="settings-note">
                  <ShieldCheck size={16} />
                  <p>
                    {t(
                      "Пароли Steam и Steam Guard не хранятся. Вход выполняется вручную в официальном клиенте. Telegram настроен только на сервере, секреты не передаются в браузер.",
                      "Steam passwords and Steam Guard secrets are not stored. Sign in manually through the official client. Telegram is configured server-side; secrets are never sent to the browser.",
                    )}
                  </p>
                </div>
              </section>
              <div className="subsection-heading">
                <h2>{t("Уведомления", "Notifications")}</h2>
              </div>
              <DataTable
                data={notifications.data}
                loading={notifications.isPending}
                error={notifications.error}
                page={page}
                onPage={setPage}
                columns={[
                  {
                    key: "created_at",
                    label: "TIME",
                    render: (r) => date(r.created_at),
                  },
                  { key: "event", label: "TYPE" },
                  {
                    key: "severity",
                    label: "LEVEL",
                    render: (r) => <Status value={r.severity} />,
                  },
                  { key: "message", label: "MESSAGE" },
                  {
                    key: "sent_at",
                    label: "SENT",
                    render: (r) => date(r.sent_at),
                  },
                ]}
              />
              {me.data?.role === "admin" && (
                <>
                  <div className="subsection-heading">
                    <h2>{t("Пользователи и роли", "Users and roles")}</h2>
                    <Button
                      size="sm"
                      onClick={() =>
                        setForm({
                          title: t("Создать пользователя", "Create user"),
                          fields: [
                            {
                              name: "username",
                              label: t("Имя пользователя", "Username"),
                              required: true,
                            },
                            {
                              name: "password",
                              label: t(
                                "Пароль (минимум 14 символов)",
                                "Password (at least 14 characters)",
                              ),
                              type: "password",
                              required: true,
                            },
                            {
                              name: "role",
                              label: t("Роль", "Role"),
                              type: "select",
                              required: true,
                              value: "viewer",
                              options: ["admin", "operator", "viewer"].map(
                                (value) => ({ value, label: value }),
                              ),
                            },
                          ],
                          submit: (v) =>
                            api("/users", {
                              method: "POST",
                              body: JSON.stringify(v),
                            }),
                        })
                      }
                    >
                      <Plus size={14} />
                      {t("Добавить пользователя", "Add user")}
                    </Button>
                  </div>
                  <DataTable
                    {...tableProps}
                    columns={[
                      { key: "username", label: "USER" },
                      {
                        key: "role",
                        label: "ROLE",
                        render: (r) => <Status value={r.role} />,
                      },
                      {
                        key: "created_at",
                        label: "CREATED",
                        render: (r) => date(r.created_at),
                      },
                    ]}
                  />
                </>
              )}
            </>
          )}
          <footer className="main-footer">
            <span>
              CS2 Farm Manager <i />{" "}
              {t("Собственная инфраструктура", "Your infrastructure")}
            </span>
            <span>
              {t(
                "Автоматизация официальных матчей не выполняется",
                "Official match automation is never performed",
              )}
            </span>
          </footer>
        </main>
      </div>
      <ActionDialog
        spec={form}
        onClose={() => setForm(null)}
        onSuccess={(value) => {
          notify(
            t(
              "Операция принята. Проверьте обновлённое состояние.",
              "Operation accepted. Check the updated state.",
            ),
          );
          void refresh();
          if (value && typeof value === "object" && "token" in value)
            setSecret(
              `${text((value as Record<string, unknown>).token)}\nExpires: ${text((value as Record<string, unknown>).expires_at)}`,
            );
        }}
      />
      {secret && (
        <div className="info-overlay">
          <section className="info-dialog">
            <div>
              <h2>{t("Данные операции", "Operation details")}</h2>
              <button aria-label="Close details" onClick={() => setSecret("")}>
                <X size={20} />
              </button>
            </div>
            <pre>{secret}</pre>
            <Button
              variant="secondary"
              onClick={() => {
                void navigator.clipboard
                  .writeText(secret)
                  .then(() => notify(t("Скопировано", "Copied")))
                  .catch(() =>
                    notify(
                      t("Буфер обмена недоступен", "Clipboard unavailable"),
                      true,
                    ),
                  );
              }}
            >
              <Copy size={14} />
              {t("Скопировать", "Copy")}
            </Button>
          </section>
        </div>
      )}
    </div>
  );
}
