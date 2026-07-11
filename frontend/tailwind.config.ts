import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: { extend: { colors: { ink: "#070b12", panel: "#0d1420", line: "#1d2a3a", cyan: "#38d9c5" } } },
  plugins: []
} satisfies Config;

