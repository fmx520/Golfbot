import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Golfbot",
  description: "Minimal speed-first Golfbot web UI",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
