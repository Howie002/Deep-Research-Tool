import type { Metadata } from "next";
import "./globals.css";
import AppShell from "@/components/layout/AppShell";

export const metadata: Metadata = {
  title: "Deep Research Agent | Texas A&M Foundation",
  description: "Multi-agent deep research on Foundation local inference: plan, search, read, synthesize, and cite.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="h-full antialiased">
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: "try{if(localStorage.getItem('tamf-theme')==='dark'){document.documentElement.dataset.theme='dark';}}catch(e){}",
          }}
        />
      </head>
      <body className="h-full">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
