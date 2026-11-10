"use client";

import Link from "next/link";

import { useMobileMenu } from "@/hooks/use-mobile-menu";
import { navigationItems, site } from "@/lib/site";

export function SiteNavigation() {
  const { isOpen, close, toggle } = useMobileMenu();

  return (
    <header className="border-b border-emerald-950/10 bg-white">
      <div className="mx-auto flex min-h-18 w-full max-w-6xl items-center justify-between gap-6 px-5 sm:px-8">
        <Link
          aria-label={`${site.name} - الرئيسية`}
          className="shrink-0 text-lg font-bold text-emerald-950 sm:text-xl"
          href="/"
          onClick={close}
        >
          {site.shortName}
        </Link>

        <button
          aria-controls="primary-navigation"
          aria-expanded={isOpen}
          aria-label={isOpen ? "إغلاق القائمة" : "فتح القائمة"}
          className="inline-flex size-11 items-center justify-center rounded-xl border border-slate-200 text-xl text-emerald-950 sm:hidden"
          onClick={toggle}
          type="button"
        >
          <span aria-hidden="true">{isOpen ? "×" : "☰"}</span>
        </button>

        <nav
          aria-label="التنقل الرئيسي"
          className={`${isOpen ? "flex" : "hidden"} absolute inset-x-0 top-18 z-10 flex-col gap-1 border-b border-emerald-950/10 bg-white p-4 shadow-sm sm:static sm:flex sm:flex-row sm:items-center sm:gap-2 sm:border-0 sm:bg-transparent sm:p-0 sm:shadow-none`}
          id="primary-navigation"
        >
          {navigationItems.map((item) => (
            <Link
              className="rounded-full px-4 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-emerald-50 hover:text-emerald-900 focus-visible:outline-2 focus-visible:outline-emerald-800"
              href={item.href}
              key={item.href}
              onClick={close}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}
