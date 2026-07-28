'use client';

import { LayoutGrid, Telescope } from 'lucide-react';
import { usePathname } from 'next/navigation';
import ThemeToggle from './ThemeToggle';
import SidebarTutorialButton from '@/components/tour/SidebarTutorialButton';
import FeedbackButton from '@/components/FeedbackButton';

const BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? '';

export default function Sidebar() {
  const pathname = usePathname();
  const isHome = pathname === '/';

  return (
    <aside data-nav-rail className="group flex flex-col w-16 hover:w-[220px] transition-all duration-200 ease-in-out bg-[#500000] border-r border-[#3c001c] min-h-screen overflow-hidden shrink-0">
      <div className="flex items-center h-14 px-3 border-b border-[#3c001c] bg-[#3c001c] shrink-0">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={`${BASE}/tamu-foundation-logo.png`} alt="Texas A&M Foundation" className="h-10 w-10 object-contain shrink-0" />
        <span className="text-white font-bold text-sm whitespace-nowrap ml-2 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
          Deep Research
        </span>
      </div>

      <nav className="flex flex-col gap-1 p-2 flex-1">
        <a href="/" className="flex items-center gap-3 px-3 py-2.5 rounded-md text-white/70 hover:bg-white/10 hover:text-white transition-colors duration-150">
          <LayoutGrid size={20} className="shrink-0" strokeWidth={1.75} />
          <span className="text-sm font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-150">Home</span>
        </a>
        <a href={`${BASE}/`} className={`flex items-center gap-3 px-3 py-2.5 rounded-md transition-colors duration-150 ${isHome ? 'bg-[#3c001c] text-white' : 'text-white/70 hover:bg-white/10 hover:text-white'}`}>
          <Telescope size={20} className="shrink-0" strokeWidth={isHome ? 2.5 : 1.75} />
          <span className="text-sm font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-150">Research</span>
        </a>
      </nav>

      <div className="p-2 border-t border-[#3c001c] shrink-0 space-y-1">
        <SidebarTutorialButton />
        <FeedbackButton />
        <ThemeToggle />
      </div>
    </aside>
  );
}
