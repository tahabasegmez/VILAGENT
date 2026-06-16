import { Toaster } from "sonner";

import { ComputerUseOperatorConsole } from "@/components/computer-use/operator-console";
import { QueryClientProvider } from "@/components/query-client-provider";

export default function VilagentOperatorPage() {
  return (
    <QueryClientProvider>
      <ComputerUseOperatorConsole />
      <Toaster position="top-center" />
    </QueryClientProvider>
  );
}
