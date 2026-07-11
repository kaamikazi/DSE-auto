import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/Sidebar";
import { StatusBanners } from "@/components/StatusBanners";

export const metadata: Metadata = { title: "DSE AutoTrader", description: "Supervised DSE research and paper trading" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><div className="flex min-h-screen"><Sidebar/><div className="min-w-0 flex-1"><StatusBanners/>{children}</div></div></body></html>;
}

