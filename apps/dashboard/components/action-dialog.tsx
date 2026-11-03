"use client";
import { useEffect, useState } from "react";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import type { Field } from "@/types";
export type FormSpec = {
  title: string;
  description?: string;
  fields: Field[];
  submit: (
    values: Record<string, string | number | boolean>,
  ) => Promise<unknown>;
  destructive?: boolean;
  submitLabel?: string;
};
export function ActionDialog({
  spec,
  onClose,
  onSuccess,
}: {
  spec: FormSpec | null;
  onClose: () => void;
  onSuccess: (value: unknown) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setError("");
  }, [spec]);
  return (
    <Dialog
      open={!!spec}
      onOpenChange={(v) => {
        if (!v && !busy) {
          setError("");
          onClose();
        }
      }}
    >
      {spec && (
        <DialogContent title={spec.title} description={spec.description}>
          <form
            key={spec.title}
            onSubmit={async (e) => {
              e.preventDefault();
              const form = e.currentTarget;
              const data = new FormData(form);
              const values: Record<string, string | number | boolean> = {};
              for (const f of spec.fields) {
                const v = data.get(f.name);
                values[f.name] =
                  f.type === "number"
                    ? v === ""
                      ? ""
                      : Number(v)
                    : f.type === "checkbox"
                      ? v === "on"
                      : String(v ?? "");
              }
              setBusy(true);
              setError("");
              try {
                const result = await spec.submit(values);
                onSuccess(result);
                onClose();
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <div className="form-fields">
              {spec.fields.map((f) => (
                <label className="field" key={f.name}>
                  <span>
                    {f.label}
                    {f.required && " *"}
                  </span>
                  {f.type === "select" ? (
                    <select
                      name={f.name}
                      required={f.required}
                      defaultValue={String(f.value ?? "")}
                    >
                      <option value="">—</option>
                      {f.options?.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  ) : f.type === "textarea" ? (
                    <textarea
                      name={f.name}
                      required={f.required}
                      defaultValue={String(f.value ?? "")}
                      rows={4}
                    />
                  ) : (
                    <input
                      name={f.name}
                      type={f.type || "text"}
                      required={f.required}
                      defaultValue={
                        f.type === "checkbox"
                          ? undefined
                          : String(f.value ?? "")
                      }
                      defaultChecked={
                        f.type === "checkbox" ? Boolean(f.value) : undefined
                      }
                      min={f.min}
                      step={f.step}
                      autoComplete={
                        f.type === "password" ? "new-password" : undefined
                      }
                    />
                  )}
                  {f.hint && <small>{f.hint}</small>}
                </label>
              ))}
            </div>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            <div className="dialog-actions">
              <Button
                type="button"
                variant="secondary"
                disabled={busy}
                onClick={onClose}
              >
                Cancel / Отмена
              </Button>
              <Button
                type="submit"
                variant={spec.destructive ? "destructive" : "default"}
                disabled={busy}
              >
                {busy
                  ? "Processing / Выполняется…"
                  : spec.submitLabel || "Confirm / Подтвердить"}
              </Button>
            </div>
          </form>
        </DialogContent>
      )}
    </Dialog>
  );
}
