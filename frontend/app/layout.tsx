import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";

import "./globals.css";
import { Providers } from "./providers";

// This runs on localhost, so these are really just the browser tab title. Kept
// accurate rather than promotional -- there is no public page to preview.
export const metadata: Metadata = {
  title: "AI Job Hunter",
  description:
    "Reads job listings through three layers of AI and hands back the few worth "
    + "your time, each with its reasoning attached: what the role really is, what "
    + "the employer screens on, and whether you meet the bar. Ghost listings "
    + "flagged, visa sponsors checked, dead listings removed.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
