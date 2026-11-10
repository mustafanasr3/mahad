import Link from "next/link";

import { site } from "@/lib/site";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col justify-center px-5 py-16 sm:px-8 sm:py-24">
      <section
        aria-labelledby="welcome-title"
        className="rounded-3xl border border-emerald-950/10 bg-white p-8 shadow-sm sm:p-14"
      >
        <p className="mb-4 text-sm font-semibold tracking-wide text-emerald-800">
          مكتبة العلوم الشرعية
        </p>
        <h1
          id="welcome-title"
          className="max-w-3xl text-4xl font-bold leading-tight text-emerald-950 sm:text-6xl"
        >
          {site.name}
        </h1>
        <p className="mt-6 max-w-2xl text-lg leading-9 text-slate-600">
          {site.description}
        </p>
        <p className="mt-4 max-w-2xl leading-8 text-slate-600">
          هذه هي النسخة الجديدة قيد الإعداد. ستُنقل وظائف الموقع ومحتواه
          تدريجيًا بعد مراجعة كل مرحلة، مع إبقاء التطبيق وقاعدة البيانات
          الأصليين دون تغيير.
        </p>
        <Link
          className="mt-8 inline-flex min-h-12 items-center justify-center rounded-full bg-emerald-900 px-6 py-3 font-semibold text-white transition-colors hover:bg-emerald-800 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-emerald-800"
          href="#library"
        >
          تعرّف على النسخة الجديدة
        </Link>
      </section>

      <section
        aria-labelledby="library-title"
        className="grid gap-6 py-12 sm:grid-cols-2 sm:py-16"
        id="library"
      >
        <div className="rounded-2xl border border-slate-200 bg-white p-7">
          <h2 id="library-title" className="text-xl font-bold text-emerald-950">
            أساس جديد، خطوة بخطوة
          </h2>
          <p className="mt-3 leading-8 text-slate-600">
            أُعدّ التطبيق باستخدام Next.js وTypeScript ليكون جاهزًا لتصدير
            الملفات الثابتة واستضافة GitHub Pages.
          </p>
        </div>
        <div
          aria-labelledby="about-title"
          className="rounded-2xl border border-slate-200 bg-white p-7"
          id="about"
        >
          <h2 id="about-title" className="text-xl font-bold text-emerald-950">
            لا تغيير على النسخة الأصلية
          </h2>
          <p className="mt-3 leading-8 text-slate-600">
            لم تُنقل بيانات أو وظائف بعد. ستبقى ملفات Python وقاعدة البيانات
            كما هي خلال مراحل الانتقال.
          </p>
        </div>
      </section>
    </main>
  );
}
