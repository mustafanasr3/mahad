"use client";

import { useState } from "react";

export function useMobileMenu() {
  const [isOpen, setIsOpen] = useState(false);

  return {
    isOpen,
    close: () => setIsOpen(false),
    toggle: () => setIsOpen((open) => !open),
  };
}
