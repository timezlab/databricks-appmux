import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DataTable } from "@/components/data-table";

interface Row {
  slug: string;
  n: number;
}

const columns = [
  {
    key: "slug",
    header: "App",
    cell: (row: Row) => row.slug,
    isRowHeader: true,
  },
  {
    key: "n",
    header: "Memory",
    cell: (row: Row) => String(row.n),
    align: "end" as const,
  },
];

describe("DataTable", () => {
  it("is a named table with column headers and row headers", () => {
    render(
      <DataTable
        caption="Apps"
        columns={columns}
        rows={[{ slug: "sales", n: 3 }]}
        rowKey={(row) => row.slug}
      />,
    );
    const table = screen.getByRole("table", { name: "Apps" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual(["App", "Memory"]);
    expect(
      within(table).getByRole("rowheader", { name: "sales" }),
    ).toBeInTheDocument();
    expect(within(table).getByRole("cell", { name: "3" })).toBeInTheDocument();
  });

  it("still names the table when the caption is hidden", () => {
    render(
      <DataTable
        caption="Apps"
        isCaptionHidden
        columns={columns}
        rows={[{ slug: "sales", n: 3 }]}
        rowKey={(row) => row.slug}
      />,
    );
    expect(screen.getByRole("table", { name: "Apps" })).toBeInTheDocument();
  });

  it("shows the empty state, not an empty table", () => {
    render(
      <DataTable
        caption="Apps"
        columns={columns}
        rows={[]}
        rowKey={(row) => row.slug}
      />,
    );
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText("Nothing to show")).toBeInTheDocument();
  });

  it("renders a string cell as text, never as markup", () => {
    render(
      <DataTable
        caption="Apps"
        columns={columns}
        rows={[{ slug: "<img src=x>", n: 1 }]}
        rowKey={(row) => row.slug}
      />,
    );
    expect(screen.getByRole("rowheader")).toHaveTextContent("<img src=x>");
    expect(document.querySelector("img")).toBeNull();
  });

  it("puts the table in a named, focusable region, so the keyboard can scroll it sideways", () => {
    render(
      <DataTable
        caption="Apps"
        columns={columns}
        rows={[{ slug: "sales", n: 3 }]}
        rowKey={(row) => row.slug}
      />,
    );
    const frame = screen.getByRole("region", { name: "Apps" });
    expect(frame).toHaveAttribute("tabindex", "0");
    expect(within(frame).getByRole("table", { name: "Apps" })).toBeVisible();
  });

  describe("stacked layout (the default), for a phone", () => {
    const stackedColumns = [
      ...columns,
      {
        key: "actions",
        header: "Actions",
        cell: () => "Edit",
        isLabelHiddenWhenStacked: true,
      },
    ];

    it("labels each cell with its column header, so a row reads as a list on a narrow screen", () => {
      render(
        <DataTable
          caption="Apps"
          columns={stackedColumns}
          rows={[{ slug: "sales", n: 3 }]}
          rowKey={(row) => row.slug}
        />,
      );
      expect(screen.getByRole("cell", { name: "3" })).toHaveAttribute(
        "data-label",
        "Memory",
      );
    });

    it("leaves the row header and a column that asks for it without a label", () => {
      render(
        <DataTable
          caption="Apps"
          columns={stackedColumns}
          rows={[{ slug: "sales", n: 3 }]}
          rowKey={(row) => row.slug}
        />,
      );
      expect(screen.getByRole("rowheader")).not.toHaveAttribute("data-label");
      expect(screen.getByRole("cell", { name: "Edit" })).not.toHaveAttribute(
        "data-label",
      );
    });

    it("keeps the table semantics by name, since a phone layout changes how the parts are drawn", () => {
      render(
        <DataTable
          caption="Apps"
          columns={stackedColumns}
          rows={[{ slug: "sales", n: 3 }]}
          rowKey={(row) => row.slug}
        />,
      );
      expect(screen.getByRole("table", { name: "Apps" })).toBeInTheDocument();
      expect(screen.getAllByRole("row")).toHaveLength(2);
      expect(screen.getAllByRole("columnheader")).toHaveLength(3);
    });
  });

  it("labels nothing in the scroll layout, which keeps its columns on a phone and scrolls", () => {
    render(
      <DataTable
        caption="Apps"
        layout="scroll"
        columns={columns}
        rows={[{ slug: "sales", n: 3 }]}
        rowKey={(row) => row.slug}
      />,
    );
    expect(screen.getByRole("cell", { name: "3" })).not.toHaveAttribute(
      "data-label",
    );
  });
});
