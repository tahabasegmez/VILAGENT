import "@/styles/globals.css";
import "katex/dist/katex.min.css";

import { type Metadata } from "next";
import { Space_Grotesk, JetBrains_Mono } from "next/font/google";

import { ThemeProvider } from "@/components/theme-provider";

// Agentic, slightly technical type system: Space Grotesk for UI, JetBrains Mono for
// code/metrics/logs. Exposed as CSS variables consumed by globals.css.
const fontSans = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-agentic-sans",
  display: "swap",
});

const fontMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-agentic-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "VILAGENT",
  description: "A Windows-first computer-use agent operator.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${fontSans.variable} ${fontMono.variable}`} suppressContentEditableWarning suppressHydrationWarning>
      <body suppressHydrationWarning>
        <ThemeProvider attribute="class" enableSystem disableTransitionOnChange>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
