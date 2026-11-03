"use client";
import * as React from "react";
import * as Primitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
export const Dialog = Primitive.Root;
export function DialogContent({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <Primitive.Portal>
      <Primitive.Overlay className="dialog-overlay" />
      <Primitive.Content className="dialog-content">
        <Primitive.Title className="dialog-title">{title}</Primitive.Title>
        <Primitive.Description className="dialog-description">
          {description || title}
        </Primitive.Description>
        {children}
        <Primitive.Close className="dialog-close" aria-label="Close">
          <X size={18} />
        </Primitive.Close>
      </Primitive.Content>
    </Primitive.Portal>
  );
}
