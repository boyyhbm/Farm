import type { Metadata } from "next";
import "./globals.css";
import Providers from "@/components/providers";
export const metadata: Metadata = {
  title: "CS2 Farm Manager",
  description: "Distributed Steam infrastructure control panel",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ru">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
