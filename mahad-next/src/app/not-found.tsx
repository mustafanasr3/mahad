import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col items-center justify-center px-5 py-20 text-center">
      <p className="text-sm font-semibold text-emerald-800">404</p>
      <h1 className="mt-3 text-3xl font-bold text-emerald-950">
        الصفحة غير موجودة
      </h1>
      <p className="mt-4 leading-8 text-slate-600">
        لم نعثر على الصفحة المطلوبة.
      </p>
      <Link
        className="mt-7 rounded-full bg-emerald-900 px-6 py-3 font-semibold text-white hover:bg-emerald-800 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-emerald-800"
        href="/"
      >
        العودة إلى الرئيسية
      </Link>
    </main>
  );
}
