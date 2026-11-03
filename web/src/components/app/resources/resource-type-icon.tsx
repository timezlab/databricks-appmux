import { cn } from "cn";
import {
  AppWindowIcon,
  BoxIcon,
  BrainCircuitIcon,
  DatabaseZapIcon,
  FlaskConicalIcon,
  HardDriveIcon,
  KeyRoundIcon,
  PlugIcon,
  RouteIcon,
  SparklesIcon,
  SquareFunctionIcon,
  TableIcon,
  TextSearchIcon,
  WarehouseIcon,
  WorkflowIcon,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

// Presentation only: the catalogue (names, labels, levels) is the server's.
// A type this map does not know gets the plain box.
const TYPE_ICON: Readonly<Record<string, LucideIcon>> = {
  sql_warehouse: WarehouseIcon,
  secret: KeyRoundIcon,
  mlflow_experiment: FlaskConicalIcon,
  serving_endpoint: BrainCircuitIcon,
  lakebase_database: DatabaseZapIcon,
  job: WorkflowIcon,
  uc_table: TableIcon,
  uc_volume: HardDriveIcon,
  uc_function: SquareFunctionIcon,
  uc_connection: PlugIcon,
  genie_space: SparklesIcon,
  vector_search_index: TextSearchIcon,
  databricks_app: AppWindowIcon,
  gateway_model_service: RouteIcon,
};

/** The icon of a resource type; the type is named beside it in words, so the icon is hidden from screen readers. */
export function ResourceTypeIcon({
  type,
  className,
}: {
  type: string;
  className?: string;
}) {
  const Icon = TYPE_ICON[type] ?? BoxIcon;
  return (
    <Icon
      aria-hidden="true"
      className={cn("size-4 shrink-0 text-muted-foreground", className)}
    />
  );
}
