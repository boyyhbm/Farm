"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Download,
  ExternalLink,
  RefreshCw,
  Server,
  ShieldCheck,
  Stethoscope,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, download } from "@/services/api";

export type SystemStatus = {
  version: string;
  mode: "desktop" | "server";
  backend: { status: string; version?: string; detail?: string };
  database: { status: string; engine: string; detail?: string };
  queue: { status: string; engine: string; detail?: string };
  worker: { status: string; last_heartbeat?: string | null; detail?: string };
  agent_installer_available: boolean;
};
type Diagnostics = {
  checks: { name: string; status: string; detail: string }[];
};

declare global {
  interface Window {
    farmDesktop?: { openSystem: () => Promise<unknown>; openSteam?: () => Promise<unknown> };
  }
}

function useSystemStatus() {
  return useQuery({
    queryKey: ["system-status"],
    queryFn: () => api<SystemStatus>("/system/status"),
    refetchInterval: 15000,
  });
}

export function AgentInstaller({ ru }: { ru: boolean }) {
  const status = useSystemStatus();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const available = status.data?.agent_installer_available === true;
  async function getInstaller() {
    setBusy(true);
    setError("");
    try {
      await download("/downloads/agent", "CS2-Farm-Agent-Setup.exe");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="agent-installer">
      <Button
        variant="secondary"
        size="sm"
        disabled={!available || busy}
        onClick={() => void getInstaller()}
      >
        <Download size={14} />
        {busy
          ? ru
            ? "Загрузка…"
            : "Downloading…"
          : ru
            ? "Скачать установщик агента"
            : "Download agent installer"}
      </Button>
      {status.isPending && (
        <small role="status">
          {ru ? "Проверка установщика…" : "Checking installer availability…"}
        </small>
      )}
      {status.error && (
        <p className="system-error" role="alert">
          {status.error.message}
        </p>
      )}
      {status.data && !available && (
        <small>
          {ru
            ? "Установщик отсутствует в этой поставке сервера. Непроверенный EXE не предлагается."
            : "This server distribution has no agent installer. An unbuilt EXE is not offered."}
        </small>
      )}
      {error && (
        <p className="system-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function SystemManagement({
  ru,
  admin,
}: {
  ru: boolean;
  admin: boolean;
}) {
  const t = (r: string, en: string) => (ru ? r : en);
  const status = useSystemStatus();
  const [desktop, setDesktop] = useState(false);
  const [desktopError, setDesktopError] = useState("");
  const [opening, setOpening] = useState(false);
  const diagnostics = useQuery({
    queryKey: ["system-diagnostics"],
    queryFn: () => api<Diagnostics>("/system/diagnostics"),
    enabled: false,
    retry: false,
    refetchInterval: false,
  });
  useEffect(() => {
    setDesktop(typeof window.farmDesktop?.openSystem === "function");
  }, []);
  async function openSystem() {
    setDesktopError("");
    setOpening(true);
    try {
      if (!window.farmDesktop)
        throw new Error(
          t(
            "Откройте System Management из меню настольного приложения.",
            "Open System Management from the desktop application menu.",
          ),
        );
      await window.farmDesktop.openSystem();
    } catch (cause) {
      setDesktopError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setOpening(false);
    }
  }
  const rows = status.data
    ? [
        {
          label: "Backend API",
          value: status.data.backend.status,
          detail: status.data.backend.detail || status.data.backend.version,
        },
        {
          label: t("База данных", "Database"),
          value: status.data.database.status,
          detail: status.data.database.detail || status.data.database.engine,
        },
        {
          label: t("Очередь заданий", "Task queue"),
          value: status.data.queue.status,
          detail: status.data.queue.detail || status.data.queue.engine,
        },
        {
          label: t("Фоновые процессы", "Background worker"),
          value: status.data.worker.status,
          detail:
            status.data.worker.detail || status.data.worker.last_heartbeat,
        },
      ]
    : [];
  return (
    <section
      className="panel system-management"
      aria-labelledby="system-management-heading"
    >
      <div className="panel-heading">
        <div>
          <h2 id="system-management-heading">System Management</h2>
          <p>
            {t(
              "Состояние компонентов, диагностика и обслуживание.",
              "Component health, diagnostics and maintenance.",
            )}
          </p>
        </div>
        <Server size={20} />
      </div>
      <div className="system-management-body">
        {status.isPending && (
          <p role="status">
            {t("Проверяем компоненты…", "Checking components…")}
          </p>
        )}
        {status.error && (
          <div className="error-banner" role="alert">
            {t(
              "Состояние компонентов недоступно: ",
              "Component status unavailable: ",
            )}
            {status.error.message}
          </div>
        )}
        {status.data && (
          <>
            <div className="system-version">
              <span>
                {t("Версия приложения", "Application version")}{" "}
                <strong>{status.data.version}</strong>
              </span>
              <span>
                {t("Режим", "Mode")}:{" "}
                <strong>
                  {status.data.mode === "desktop"
                    ? t("Локальный Desktop", "Local desktop")
                    : t("Центральный сервер", "Central server")}
                </strong>
              </span>
            </div>
            <div className="system-health-grid">
              {rows.map((row) => (
                <article key={row.label}>
                  <span>{row.label}</span>
                  <strong className={`system-health-${row.value}`}>
                    {row.value}
                  </strong>
                  {row.detail && <small>{row.detail}</small>}
                </article>
              ))}
            </div>
          </>
        )}
        <div className="system-actions">
          <Button
            variant="secondary"
            size="sm"
            disabled={status.isFetching}
            onClick={() => void status.refetch()}
          >
            <RefreshCw size={14} />
            {t("Обновить состояние", "Refresh status")}
          </Button>
          {admin && (
            <Button
              variant="secondary"
              size="sm"
              disabled={diagnostics.isFetching}
              onClick={() => void diagnostics.refetch()}
            >
              <Stethoscope size={14} />
              {diagnostics.isFetching
                ? t("Проверяем…", "Checking…")
                : t("Диагностика соединения", "Connection diagnostics")}
            </Button>
          )}
          {desktop && (
            <Button
              size="sm"
              disabled={opening}
              onClick={() => void openSystem()}
            >
              <ExternalLink size={14} />
              {t("Открыть обслуживание", "Open maintenance")}
            </Button>
          )}
        </div>
        <p className="system-explanation">
          {desktop
            ? t(
                "В окне обслуживания доступны перезапуск компонентов, резервное копирование, восстановление, ошибки и проверка обновлений. Файлы выбираются стандартными окнами Windows.",
                "The maintenance window provides component restart, backup, restore, errors and update checks. Select files using standard Windows dialogs.",
              )
            : t(
                "Резервное копирование, восстановление, перезапуск и обновления доступны через System Management в меню настольного приложения. Веб-версия сохраняет доступ к данным и диагностике; привилегии компьютера браузеру не передаются.",
                "Backup, restore, restart and updates are available in System Management in the desktop application menu. The web dashboard retains data and diagnostics; it does not receive computer privileges.",
              )}
        </p>
        {desktopError && (
          <p className="system-error" role="alert">
            {desktopError}
          </p>
        )}
        {diagnostics.error && (
          <p className="system-error" role="alert">
            {diagnostics.error.message}
          </p>
        )}
        {diagnostics.data && (
          <div className="system-diagnostics" aria-live="polite">
            <h3>{t("Результаты диагностики", "Diagnostics results")}</h3>
            <dl>
              {diagnostics.data.checks.map((check, index) => (
                <div key={`${check.name}-${index}`}>
                  <dt>{check.name}</dt>
                  <dd>
                    <strong>{check.status}</strong>
                    <span>{check.detail}</span>
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        )}
        <div className="settings-note">
          <ShieldCheck size={16} />
          <p>
            {t(
              "Локальные компоненты не открывают доступные из сети порты автоматически. Другие ПК подключаются к доверенному HTTPS-серверу; адрес localhost доступен только текущему компьютеру.",
              "Local components do not automatically expose network ports. Other PCs connect to a trusted HTTPS server; localhost is accessible only on this computer.",
            )}
          </p>
        </div>
        <AgentInstaller ru={ru} />
      </div>
    </section>
  );
}
