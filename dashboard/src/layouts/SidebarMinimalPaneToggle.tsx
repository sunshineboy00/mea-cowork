import { ArrowLeftRight } from "lucide-react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";
import type { MinimalNavPane } from "./layoutModeStorage";
import styles from "./Sidebar.module.less";

export default function SidebarMinimalPaneToggle({
  settingsLabel,
  minimalPane,
  collapsed,
  onSelect,
}: {
  /** Name of the first navigation group. */
  settingsLabel: string;
  minimalPane: MinimalNavPane;
  /** Icon-only control when the rail is collapsed (desktop). */
  collapsed: boolean;
  onSelect: (pane: MinimalNavPane, opts?: { expand?: boolean }) => void;
}) {
  const { t } = useTranslation();
  const nextPane: MinimalNavPane =
    minimalPane === "records" ? "settings" : "records";
  const nextLabel =
    nextPane === "settings" ? settingsLabel : t("nav.paneRecords");

  if (collapsed) {
    return (
      <div className={styles.minimalPaneToggleCollapsed}>
        <Tooltip title={nextLabel} placement="right">
          <button
            type="button"
            className={styles.minimalPaneIconBtn}
            onClick={() => onSelect(nextPane, { expand: true })}
            aria-label={nextLabel}
          >
            <ArrowLeftRight size={16} strokeWidth={1.8} />
          </button>
        </Tooltip>
      </div>
    );
  }

  return (
    <div className={styles.minimalPaneHeader}>
      <span className={styles.navGroupLabel}>
        {minimalPane === "records" ? t("nav.paneRecords") : settingsLabel}
      </span>
      <Tooltip title={nextLabel} placement="top">
        {/* Native button: antd's text Button injects its own hover colors
            (global colorText/Fill) at runtime, overriding the sidebar theme
            vars and breaking dark/light adaptation. The collapsed branch's
            native button never had this problem. */}
        <button
          type="button"
          className={styles.minimalPaneSwitchBtn}
          onClick={() => onSelect(nextPane)}
          aria-label={nextLabel}
        >
          <ArrowLeftRight size={15} strokeWidth={1.8} aria-hidden />
          <span>{nextLabel}</span>
        </button>
      </Tooltip>
    </div>
  );
}
