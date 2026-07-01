import Sidebar from './Sidebar';
import TourProvider from '@/components/tour/TourProvider';

export default function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <TourProvider>
      <div className="flex min-h-screen bg-[#F6F6F6] dark:bg-zinc-950">
        <Sidebar />
        <main className="flex-1 min-w-0 overflow-auto">{children}</main>
      </div>
    </TourProvider>
  );
}
