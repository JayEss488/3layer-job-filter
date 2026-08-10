import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";

import "./globals.css";
import { Providers } from "./providers";

// The homepage is now public, so these are what a link preview and a search
// result show. Kept in the product's own register — what it does and how long
// it takes — rather than the category label ("AI job matching") the previous
// description used, which described every tool the landing page argues against.
export const metadata: Metadata = {
  title: "Four in a Thousand — job search that finds the few roles that fit",
  description:
    "An overengineered job search. Each run reads 480 listings through three layers of AI and hands back a dozen worth your time, each with a one-line verdict on what the job really is and whether you meet the bar. Ghost listings flagged, visa sponsors checked.",
  openGraph: {
    type: "website",
    siteName: "Four in a Thousand",
    title: "Four in a Thousand — job search that finds the few roles that fit",
    description:
      "Most job tools help you apply to everything. We read 480 listings a run and hand back a dozen that fit, with the reasoning attached.",
  },
  twitter: {
    card: "summary",
    title: "Four in a Thousand — job search that finds the few roles that fit",
    description:
      "Most job tools help you apply to everything. We read 480 listings a run and hand back a dozen that fit, with the reasoning attached.",
  },
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
