import type { NavigationItem } from "@/types/site";

export const site = {
  name: "مالك بن رضا المحمدي",
  shortName: "معهد مالك",
  description:
    "قناة علمية تنشر فيها برامج ودروس وفوائد الشيخ د. مالك المحمدي - وفَّقه الله -.",
} as const;

export const navigationItems = [
  { href: "#library", label: "المكتبة" },
  { href: "#about", label: "عن المشروع" },
] as const satisfies readonly NavigationItem[];
