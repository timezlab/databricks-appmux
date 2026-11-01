/**
 * What every dashboard chart shares: where the plot sits and how its axes and
 * grid look. Colours are tokens of globals.css written as presentation
 * attributes (`fill="var(--muted-foreground)"`): a style attribute would be
 * refused by the strict CSP, and an attribute is not one.
 */

/** Room left of the plot for the tick labels ("20K", "10,000"). */
export const Y_AXIS_WIDTH = 56;
export const PLOT_MARGIN = { top: 16, right: 16, bottom: 0, left: 0 } as const;

export const AXIS_TICK = { fill: "var(--muted-foreground)", fontSize: 12 };
/** The baseline (the zero line) is drawn heavier than the grid, in the control border colour. */
export const AXIS_LINE = { stroke: "var(--input)" };
export const GRID_STROKE = "var(--border)";
/** The band over a stretch of minutes with no data. */
export const GAP_FILL = "var(--muted)";
export const LABEL_FILL = "var(--foreground)";
/** The ring around a marker, so it stands out from the line it sits on. */
export const MARKER_RING = "var(--background)";
/** The band that follows the pointer over a column. */
export const CURSOR_BAND = { fill: "var(--accent)" };
/** The line that follows the pointer over a point. */
export const CURSOR_LINE = { stroke: "var(--input)" };

/** Never narrower than a readable chart (the figure scrolls sideways below it); above that it fills the page column, as the tables and tiles do. */
export const CHART_SIZE_CLASS = "min-w-[32rem]";
