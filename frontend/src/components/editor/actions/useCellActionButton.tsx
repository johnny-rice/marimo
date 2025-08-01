/* Copyright 2024 Marimo. All rights reserved. */

import type { EditorView } from "@codemirror/view";
import { useAtomValue, useSetAtom } from "jotai";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronsDownIcon,
  ChevronsUpIcon,
  ChevronUpIcon,
  Code2Icon,
  Columns2Icon,
  DatabaseIcon,
  EyeIcon,
  EyeOffIcon,
  ImageIcon,
  LinkIcon,
  PlayIcon,
  PlusCircleIcon,
  ScissorsIcon,
  SparklesIcon,
  TextCursorInputIcon,
  Trash2Icon,
  XCircleIcon,
  ZapIcon,
  ZapOffIcon,
} from "lucide-react";
import { downloadCellOutput } from "@/components/export/export-output-button";
import { MultiIcon } from "@/components/icons/multi-icon";
import { useImperativeModal } from "@/components/modal/ImperativeModal";
import {
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/use-toast";
import { aiCompletionCellAtom } from "@/core/ai/state";
import { maybeAddMarimoImport } from "@/core/cells/add-missing-import";
import { hasOnlyOneCellAtom, useCellActions } from "@/core/cells/cells";
import type { CellId } from "@/core/cells/ids";
import type { CellData } from "@/core/cells/types";
import { formatEditorViews } from "@/core/codemirror/format";
import { toggleToLanguage } from "@/core/codemirror/language/commands";
import { switchLanguage } from "@/core/codemirror/language/extension";
import {
  aiEnabledAtom,
  appWidthAtom,
  autoInstantiateAtom,
} from "@/core/config/config";
import { kioskModeAtom } from "@/core/mode";
import { saveCellConfig } from "@/core/network/requests";
import type { CellConfig, RuntimeState } from "@/core/network/types";
import { canLinkToCell, createCellLink } from "@/utils/cell-urls";
import { copyToClipboard } from "@/utils/copy";
import { MarkdownIcon, PythonIcon } from "../cell/code/icons";
import { useDeleteCellCallback } from "../cell/useDeleteCell";
import { useRunCell } from "../cell/useRunCells";
import { useSplitCellCallback } from "../cell/useSplitCell";
import { NameCellInput } from "./name-cell-input";
import type { ActionButton } from "./types";

export interface CellActionButtonProps
  extends Pick<CellData, "name" | "config"> {
  cellId: CellId;
  status: RuntimeState;
  hasOutput: boolean;
  hasConsoleOutput: boolean;
  getEditorView: () => EditorView | null;
}

interface Props {
  cell: CellActionButtonProps | null;
}

export function useCellActionButtons({ cell }: Props) {
  const {
    createNewCell: createCell,
    updateCellConfig,
    updateCellName,
    moveCell,
    sendToTop,
    sendToBottom,
    addColumnBreakpoint,
    clearCellOutput,
  } = useCellActions();
  const splitCell = useSplitCellCallback();
  const runCell = useRunCell(cell?.cellId);
  const hasOnlyOneCell = useAtomValue(hasOnlyOneCellAtom);
  const canDelete = !hasOnlyOneCell;
  const deleteCell = useDeleteCellCallback();
  const { openModal } = useImperativeModal();
  const setAiCompletionCell = useSetAtom(aiCompletionCellAtom);
  const aiEnabled = useAtomValue(aiEnabledAtom);
  const autoInstantiate = useAtomValue(autoInstantiateAtom);
  const kioskMode = useAtomValue(kioskModeAtom);
  const appWidth = useAtomValue(appWidthAtom);

  if (!cell || kioskMode) {
    return [];
  }

  const {
    cellId,
    config,
    getEditorView,
    name,
    hasOutput,
    hasConsoleOutput,
    status,
  } = cell;

  const toggleDisabled = async () => {
    const newConfig = { disabled: !config.disabled };
    await saveCellConfig({ configs: { [cellId]: newConfig } });
    updateCellConfig({ cellId, config: newConfig });
  };

  const toggleHideCode = async () => {
    const newConfig: Partial<CellConfig> = { hide_code: !config.hide_code };
    await saveCellConfig({ configs: { [cellId]: newConfig } });
    updateCellConfig({ cellId, config: newConfig });
    const editorView = getEditorView();
    // If we're hiding the code, we should blur the editor
    // otherwise, we should focus it
    if (editorView) {
      if (newConfig.hide_code) {
        editorView.contentDOM.blur();
      } else {
        editorView.focus();
      }
    }
  };

  // Actions
  const actions: ActionButton[][] = [
    [
      {
        icon: <TextCursorInputIcon size={13} strokeWidth={1.5} />,
        label: "Name",
        disableClick: true,
        handle: (evt) => {
          evt?.stopPropagation();
          evt?.preventDefault();
        },
        handleHeadless: () => {
          openModal(
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Rename cell</DialogTitle>
              </DialogHeader>
              <div className="flex items-center justify-between">
                <Label htmlFor="cell-name">Cell name</Label>
                <NameCellInput
                  placeholder={"cell name"}
                  value={name}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      e.stopPropagation();
                      openModal(null);
                    }
                  }}
                  onChange={(newName) =>
                    updateCellName({ cellId, name: newName })
                  }
                />
              </div>
            </DialogContent>,
          );
        },
        rightElement: (
          <NameCellInput
            placeholder={"cell name"}
            value={name}
            onChange={(newName) => updateCellName({ cellId, name: newName })}
          />
        ),
      },
      {
        icon: <PlayIcon size={13} strokeWidth={1.5} />,
        label: "Run cell",
        hotkey: "cell.run",
        hidden:
          status === "running" ||
          status === "queued" ||
          status === "disabled-transitively" ||
          config.disabled,
        handle: () => runCell(),
      },
      {
        icon: <SparklesIcon size={13} strokeWidth={1.5} />,
        label: "AI completion",
        hidden: !aiEnabled,
        handle: () => {
          setAiCompletionCell((current) =>
            current?.cellId === cellId ? null : { cellId },
          );
        },
        hotkey: "cell.aiCompletion",
      },
      {
        icon: <ScissorsIcon size={13} strokeWidth={1.5} />,
        label: "Split cell",
        hotkey: "cell.splitCell",
        handle: () => splitCell({ cellId }),
      },
      {
        icon: <ImageIcon size={13} strokeWidth={1.5} />,
        label: "Export output as PNG",
        hidden: !hasOutput,
        handle: () => downloadCellOutput(cellId),
      },
      {
        icon: <Code2Icon size={13} strokeWidth={1.5} />,
        label: "Format cell",
        hotkey: "cell.format",
        handle: () => {
          const editorView = getEditorView();
          if (!editorView) {
            return;
          }
          formatEditorViews({ [cellId]: editorView });
        },
      },
      {
        icon: config.hide_code ? (
          <EyeIcon size={13} strokeWidth={1.5} />
        ) : (
          <EyeOffIcon size={13} strokeWidth={1.5} />
        ),
        label: config.hide_code ? "Show code" : "Hide code",
        handle: toggleHideCode,
        hotkey: "cell.hideCode",
      },
      {
        icon: config.disabled ? (
          <ZapOffIcon size={13} strokeWidth={1.5} />
        ) : (
          <ZapIcon size={13} strokeWidth={1.5} />
        ),
        label: "Reactive execution",
        rightElement: (
          <Switch
            data-testid="cell-disable-switch"
            checked={!config.disabled}
            size="sm"
            onCheckedChange={toggleDisabled}
          />
        ),
        handle: toggleDisabled,
      },
      {
        icon: <XCircleIcon size={13} strokeWidth={1.5} />,
        label: "Clear output",
        hidden: !(hasOutput || hasConsoleOutput),
        handle: () => {
          clearCellOutput({ cellId });
        },
      },
    ],

    // View as
    [
      {
        icon: <MarkdownIcon />,
        label: "Convert to Markdown",
        hotkey: "cell.viewAsMarkdown",
        handle: () => {
          const editorView = getEditorView();
          if (!editorView) {
            return;
          }
          maybeAddMarimoImport({ autoInstantiate, createNewCell: createCell });
          switchLanguage(editorView, {
            language: "markdown",
            keepCodeAsIs: false,
          });
        },
      },
      {
        icon: <DatabaseIcon size={13} strokeWidth={1.5} />,
        label: "Convert to SQL",
        handle: () => {
          const editorView = getEditorView();
          if (!editorView) {
            return;
          }
          maybeAddMarimoImport({ autoInstantiate, createNewCell: createCell });
          switchLanguage(editorView, {
            language: "sql",
            keepCodeAsIs: false,
          });
        },
      },
      {
        icon: <PythonIcon />,
        label: "Toggle as Python",
        handle: () => {
          const editorView = getEditorView();
          if (!editorView) {
            return;
          }
          maybeAddMarimoImport({ autoInstantiate, createNewCell: createCell });
          toggleToLanguage(editorView, "python", { force: true });
        },
      },
    ],

    // Movement
    [
      {
        icon: (
          <MultiIcon>
            <PlusCircleIcon size={13} strokeWidth={1.5} />
            <ChevronUpIcon size={8} strokeWidth={2} />
          </MultiIcon>
        ),
        label: "Create cell above",
        hotkey: "cell.createAbove",
        handle: () => createCell({ cellId, before: true }),
      },
      {
        icon: (
          <MultiIcon>
            <PlusCircleIcon size={13} strokeWidth={1.5} />
            <ChevronDownIcon size={8} strokeWidth={2} />
          </MultiIcon>
        ),
        label: "Create cell below",
        hotkey: "cell.createBelow",
        handle: () => createCell({ cellId, before: false }),
      },
      {
        icon: <ChevronUpIcon size={13} strokeWidth={1.5} />,
        label: "Move cell up",
        hotkey: "cell.moveUp",
        handle: () => moveCell({ cellId, before: true }),
      },
      {
        icon: <ChevronDownIcon size={13} strokeWidth={1.5} />,
        label: "Move cell down",
        hotkey: "cell.moveDown",
        handle: () => moveCell({ cellId, before: false }),
      },
      {
        icon: <ChevronLeftIcon size={13} strokeWidth={1.5} />,
        label: "Move cell left",
        hotkey: "cell.moveLeft",
        handle: () => moveCell({ cellId, direction: "left" }),
        hidden: appWidth !== "columns",
      },
      {
        icon: <ChevronRightIcon size={13} strokeWidth={1.5} />,
        label: "Move cell right",
        hotkey: "cell.moveRight",
        handle: () => moveCell({ cellId, direction: "right" }),
        hidden: appWidth !== "columns",
      },
      {
        icon: <ChevronsUpIcon size={13} strokeWidth={1.5} />,
        label: "Send to top",
        hotkey: "cell.sendToTop",
        // When using the cell menu, likely the user doesn't want to scroll
        // and instead just wants to get the cell out of the way
        handle: () => sendToTop({ cellId, scroll: false }),
      },
      {
        icon: <ChevronsDownIcon size={13} strokeWidth={1.5} />,
        label: "Send to bottom",
        hotkey: "cell.sendToBottom",
        // When using the cell menu, likely the user doesn't want to scroll
        // and instead just wants to get the cell out of the way
        handle: () => sendToBottom({ cellId, scroll: false }),
      },
      {
        icon: <Columns2Icon size={13} strokeWidth={1.5} />,
        label: "Break into new column",
        hotkey: "cell.addColumnBreakpoint",
        hidden: appWidth !== "columns",
        handle: () => addColumnBreakpoint({ cellId }),
      },
    ],

    // Link to cell
    [
      {
        icon: <LinkIcon size={13} strokeWidth={1.5} />,
        label: "Copy link to cell",
        disabled: !canLinkToCell(name),
        tooltip: canLinkToCell(name)
          ? undefined
          : "Only named cells can be linked to",
        handle: async () => {
          await copyToClipboard(createCellLink(name));
          toast({ description: "Link copied to clipboard" });
        },
      },
    ],

    // Delete
    [
      {
        label: "Delete",
        hidden: !canDelete,
        variant: "danger",
        icon: <Trash2Icon size={13} strokeWidth={1.5} />,
        handle: () => {
          deleteCell({ cellId });
        },
      },
    ],
  ];

  // remove hidden
  return actions
    .map((group) => group.filter((action) => !action.hidden))
    .filter((group) => group.length > 0);
}
