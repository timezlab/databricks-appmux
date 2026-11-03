import type { ReactNode } from "react";

import type { EgressRule } from "@/lib/api";
import { egressAddressWords, egressPortWords } from "@/lib/app/egress";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";

interface NumberedRule {
  number: number;
  rule: EgressRule;
}

const COLUMNS: readonly DataTableColumn<NumberedRule>[] = [
  {
    key: "number",
    header: "Order",
    isRowHeader: true,
    cell: ({ number }) => `Rule ${number}`,
  },
  {
    key: "action",
    header: "Action",
    cell: ({ rule }) => rule.action,
  },
  {
    key: "address",
    header: "Address",
    cell: ({ rule }) => {
      const words = egressAddressWords(rule.cidr);
      return (
        <span className="flex flex-col">
          <span className="font-mono text-[0.9em] wrap-break-word">
            {rule.cidr}
          </span>
          {words === null ? null : (
            <span className="text-sm text-muted-foreground">{words}</span>
          )}
        </span>
      );
    },
  },
  {
    key: "port",
    header: "Port",
    cell: ({ rule }) => egressPortWords(rule.port),
  },
  {
    key: "comment",
    header: "Comment",
    cell: ({ rule }) => rule.comment,
  },
];

/** An ordered list of egress rules, read-only: the first that matches decides. */
export function EgressRulesTable({
  caption,
  rules,
  empty,
}: {
  caption: string;
  rules: readonly EgressRule[];
  /** Shown instead of the table when the list is empty: say what decides instead. */
  empty: ReactNode;
}) {
  return (
    <DataTable
      caption={caption}
      columns={COLUMNS}
      rows={rules.map((rule, index) => ({ number: index + 1, rule }))}
      rowKey={({ number }) => String(number)}
      empty={empty}
    />
  );
}
