import { site } from "@/lib/site";

export function SiteFooter() {
  return (
    <footer className="border-t border-emerald-950/10 bg-white">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-2 px-5 py-6 text-sm leading-7 text-slate-600 sm:flex-row sm:items-center sm:justify-between sm:px-8">
        <p className="font-semibold text-emerald-950">{site.name}</p>
        <p>© {new Date().getFullYear()} جميع الحقوق محفوظة</p>
      </div>
    </footer>
  );
}
